"""
Live integration diagnostics + parse-layer validation for the conflict-detection LLM call.

PURPOSE
-------
Validate the streaming content assembly against the real Vertex AI / Gemini
API without running the full framework (no Docker, no CrewAI, no FastAPI).

RUN
---
    python -m pytest tests/test_optimization/test_conflict_detection_live.py \
        -v -s -m integration

The -s flag is required: it un-captures stdout so the chunk-structure
printout is visible.  That printout is the primary diagnostic tool.

TEST-DRIVEN WORKFLOW
--------------------
1. Run → test_conflict_detection_llm_returns_valid_json FAILS
   (content is empty or RuntimeError is raised)
2. Read the CHUNK STRUCTURE printout from test_raw_stream_chunk_structure
   to see which delta field(s) actually carry the JSON answer
3. Fix _call_conflict_detection_llm to read from the correct field(s)
4. Run again → both tests PASS

WHY THIS FILE EXISTS
--------------------
The production failure path is:
  Trigger 1 fires → _call_conflict_detection_llm → stream_chunk_builder
  → choices[0].message.content = "" (the bug)
  → json.loads("") → JSONDecodeError (non-blocking warning in run.log)

This file isolates that path so it can be reproduced and fixed without
needing to trigger the full pass→fail→pass workflow via the UI.

REQUIREMENTS
------------
- Vertex AI service account credentials in env (GOOGLE_APPLICATION_CREDENTIALS
  or gcloud auth application-default login)
- OPTIMIZATION_ENABLED is forced True by the conftest.py session fixture
  already present in this package — no extra setup needed
- MODEL_PROVIDER and ONLINE_MODEL in .env must resolve to the model that
  is experiencing the issue (gemini-2.5-flash via vertex_ai)
"""

import json
import pytest


# ---------------------------------------------------------------------------
# Helper — identical to what we intend to add to the framework call sites.
# Validated here first, applied to framework code once all tests pass.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Unit tests for _parse_conflict_json — no API calls, run offline.
# These are the "make it fail → update → passes" tests for the parse layer.
# ---------------------------------------------------------------------------

class TestParseConflictJson:
    """Validate _parse_conflict_json against every format the model may emit.

    Run these first (no API needed) to confirm the helper handles all
    real-world variations before applying it to the framework call sites.
    """

    def test_plain_json(self):
        """Happy path: model returned clean JSON with no fences."""
        result = _parse_conflict_json('{"flag": [1, 2], "reason": "bad hint"}')
        assert result == {"flag": [1, 2], "reason": "bad hint"}

    def test_json_code_fence_with_language(self):
        """```json\\n...\\n``` — most common model response format."""
        content = '```json\n{"flag": [1], "reason": "viewport caused failure"}\n```'
        result = _parse_conflict_json(content)
        assert result["flag"] == [1]
        assert "viewport" in result["reason"]

    def test_json_code_fence_without_language(self):
        """``` (no language specifier) is also valid."""
        content = '```\n{"flag": [], "reason": "no conflicts"}\n```'
        result = _parse_conflict_json(content)
        assert result["flag"] == []

    def test_fence_with_uppercase_language(self):
        """```JSON (uppercase) must also be stripped."""
        content = '```JSON\n{"flag": [3], "reason": "hint 3 was harmful"}\n```'
        result = _parse_conflict_json(content)
        assert result["flag"] == [3]

    def test_fence_with_leading_trailing_whitespace(self):
        """Leading/trailing whitespace around the whole block is handled."""
        content = '  \n```json\n{"flag": [], "reason": "ok"}\n```\n  '
        result = _parse_conflict_json(content)
        assert result["flag"] == []

    def test_fence_trailing_whitespace_after_closing_fence(self):
        """Trailing spaces after ``` closing fence must not break parsing."""
        content = '```json\n{"flag": [2], "reason": "x"}\n```   '
        result = _parse_conflict_json(content)
        assert result["flag"] == [2]

    def test_empty_flag_list(self):
        """flag=[] (no conflicts) parses correctly in both plain and fenced forms."""
        plain = '{"flag": [], "reason": "no conflicts detected"}'
        fenced = '```json\n{"flag": [], "reason": "no conflicts detected"}\n```'
        assert _parse_conflict_json(plain)["flag"] == []
        assert _parse_conflict_json(fenced)["flag"] == []

    def test_truly_empty_content_raises(self):
        """Empty string must still raise JSONDecodeError (not silently return)."""
        with pytest.raises(json.JSONDecodeError):
            _parse_conflict_json("")

    def test_empty_fence_raises(self):
        """A code fence with nothing inside must also raise JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            _parse_conflict_json("```json\n```")


# ---------------------------------------------------------------------------
# Shared test data — matches the kind of Robot Framework diff that Trigger 1
# actually sees in production, rich enough to provoke extended thinking.
# ---------------------------------------------------------------------------

_FAILED_CODE = """
*** Settings ***
Library    Browser

*** Test Cases ***
Login Test
    New Browser    chromium    headless=True
    New Context    viewport=None
    New Page    https://sujal.astppbilling.org/
    Fill Text    //input[@name="username"]    admin
    Fill Text    //input[@name="password"]    secret
    Click    //button[@type="submit"]
    Get Text    //h1    ==    Dashboard
    Close Browser
""".strip()

_WORKING_CODE = """
*** Settings ***
Library    Browser

*** Test Cases ***
Login Test
    New Browser    chromium    headless=True
    New Context
    New Page    https://sujal.astppbilling.org/
    Fill Text    css=input[name="username"]    admin
    Fill Text    css=input[name="password"]    secret
    Click    css=button[type="submit"]
    Get Text    h1    ==    Dashboard
""".strip()

_ACTIVE_HINTS = [
    {"id": 1, "feedback_text": "always pass viewport=None to New Context to avoid viewport errors"},
    {"id": 2, "feedback_text": "prefer XPath over CSS selectors for robustness"},
    {"id": 3, "feedback_text": "never close the browser in the test — let cleanup handle it"},
]


def _prompt():
    from src.backend.services.workflow_service import _build_conflict_prompt
    return _build_conflict_prompt(_FAILED_CODE, _WORKING_CODE, _ACTIVE_HINTS)


# ---------------------------------------------------------------------------
# Test 1 — Raw chunk structure diagnostic
#
# Always passes. Purpose: print the exact delta fields so we can see
# where the JSON answer lands (delta.content vs delta.reasoning_content).
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_raw_stream_chunk_structure():
    """Print every streaming chunk's delta fields.

    Look for lines labelled delta.content — that is where the final JSON
    answer SHOULD arrive.  If only delta.reasoning_content lines appear and
    delta.content is never set, the answer is trapped inside the thinking
    block, which is the root cause of the empty-content bug.

    This test always passes; it exists only to emit the diagnostic output.
    """
    import litellm
    from src.backend.crew_ai.optimization.learning_config import (
        _get_conflict_detection_model,
        _get_conflict_detection_completion_kwargs,
    )

    model_string = _get_conflict_detection_model()
    extra_kwargs = _get_conflict_detection_completion_kwargs()
    prompt = _prompt()

    print(f"\n{'='*60}")
    print(f"CHUNK STRUCTURE — model: {model_string}")
    print(f"{'='*60}")

    content_parts: list = []
    reasoning_parts: list = []
    chunks: list = []

    for i, chunk in enumerate(litellm.completion(
        model=model_string,
        messages=[{"role": "user", "content": prompt}],
        timeout=90,
        stream=True,
        **extra_kwargs,
    )):
        chunks.append(chunk)
        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta
        content_frag = getattr(delta, "content", None)
        reasoning_frag = getattr(delta, "reasoning_content", None)

        if content_frag:
            content_parts.append(content_frag)
            print(f"  [{i:3d}] delta.content        = {content_frag!r:.80}")
        elif reasoning_frag:
            reasoning_parts.append(reasoning_frag)
            # Only print first/last few reasoning chunks to avoid flooding
            if len(reasoning_parts) <= 2 or not reasoning_parts:
                print(f"  [{i:3d}] delta.reasoning_content = {reasoning_frag!r:.60}...")
            elif len(reasoning_parts) == 3:
                print(f"  [{i:3d}] delta.reasoning_content = ... (suppressing middle chunks)")

    # stream_chunk_builder result
    scb_response = litellm.stream_chunk_builder(chunks, messages=[{"role": "user", "content": prompt}])
    scb_content = (
        scb_response.choices[0].message.content
        if scb_response and scb_response.choices
        else None
    )
    assembled = "".join(content_parts)

    print(f"\n--- SUMMARY ---")
    print(f"  Total chunks              : {len(chunks)}")
    print(f"  Chunks with delta.content : {len(content_parts)}")
    print(f"  Chunks with delta.reasoning_content: {len(reasoning_parts)}")
    print(f"  Manually assembled content: {assembled!r:.200}")
    print(f"  stream_chunk_builder.content: {scb_content!r:.200}")
    print(f"{'='*60}\n")

    # Diagnostic conclusions printed for easy reading
    if content_parts:
        print("  DIAGNOSIS: delta.content IS populated — assembly should work.")
    elif reasoning_parts and not content_parts:
        print(
            "  DIAGNOSIS: delta.content is NEVER populated.\n"
            "  The answer is only in delta.reasoning_content.\n"
            "  _call_conflict_detection_llm must read reasoning_content as fallback."
        )
    else:
        print("  DIAGNOSIS: Neither delta.content nor delta.reasoning_content populated.")


# ---------------------------------------------------------------------------
# Test 2 — _call_conflict_detection_llm must return parseable JSON
#
# This is the FUNCTIONAL gate. It FAILS when the bug is active and PASSES
# when the fix is correct.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_conflict_detection_llm_returns_valid_json():
    """_call_conflict_detection_llm must return non-empty, parseable JSON.

    FAILS with old code (content = "" → json.loads raises) or with a
    partial fix (both delta.content and stream_chunk_builder give empty).

    PASSES once _call_conflict_detection_llm correctly surfaces the JSON
    regardless of which delta field it arrives in.
    """
    from src.backend.crew_ai.optimization.learning_config import (
        _call_conflict_detection_llm,
        _get_conflict_detection_model,
        _get_conflict_detection_completion_kwargs,
    )

    model_string = _get_conflict_detection_model()
    extra_kwargs = _get_conflict_detection_completion_kwargs()

    response = _call_conflict_detection_llm(
        model_string=model_string,
        messages=[{"role": "user", "content": _prompt()}],
        extra_kwargs=extra_kwargs,
        timeout=90,
    )

    content = response.choices[0].message.content

    assert content, (
        "_call_conflict_detection_llm returned empty content.\n"
        "This is the bug. Run test_raw_stream_chunk_structure (with -s) to "
        "see which delta field carries the JSON and fix the helper accordingly."
    )

    try:
        result = _parse_conflict_json(content)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"Content is not valid JSON even after code-fence stripping: {exc}\n"
            f"raw content={content!r}"
        )

    assert "flag" in result, f"JSON missing 'flag' key: {result}"
    assert isinstance(result["flag"], list), f"'flag' is not a list: {result}"
    # New per-hint schema: each entry is {"id": int, "reason": str}.
    # Top-level "reason" no longer exists — reason is per-hint.
    for entry in result["flag"]:
        assert isinstance(entry, dict), f"Flag entry not a dict: {entry}"
        assert "id" in entry, f"Flag entry missing 'id': {entry}"

    print(f"\n  PASS — response: flag={result['flag']}")
