"""
Live integration diagnostics for the conflict-detection LLM call.

PURPOSE
-------
Validate the streaming content assembly against the real Vertex AI / Gemini
API without running the full framework (no Docker, no CrewAI, no FastAPI).

RUN
---
    python -m pytest tests/test_integration/test_conflict_detection_live.py \
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
- MODEL_PROVIDER and ONLINE_MODEL in .env must resolve to the model that
  is experiencing the issue (gemini-2.5-flash via vertex_ai)

For the offline unit tests of the JSON parse helper see
tests/test_optimization/test_conflict_detection_parse.py.
"""

import json
import pytest


def _parse_conflict_json(content: str) -> dict:
    """Parse JSON from the LLM response, tolerating markdown fences and preamble."""
    idx = content.find('{')
    if idx == -1:
        raise json.JSONDecodeError("No JSON object found in LLM response", content, 0)
    return json.JSONDecoder().raw_decode(content, idx)[0]


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
            if len(reasoning_parts) <= 2 or not reasoning_parts:
                print(f"  [{i:3d}] delta.reasoning_content = {reasoning_frag!r:.60}...")
            elif len(reasoning_parts) == 3:
                print(f"  [{i:3d}] delta.reasoning_content = ... (suppressing middle chunks)")

    scb_response = litellm.stream_chunk_builder(chunks, messages=[{"role": "user", "content": prompt}])
    scb_content = (
        scb_response.choices[0].message.content
        if scb_response and scb_response.choices
        else None
    )
    assembled = "".join(content_parts)

    print("\n--- SUMMARY ---")
    print(f"  Total chunks              : {len(chunks)}")
    print(f"  Chunks with delta.content : {len(content_parts)}")
    print(f"  Chunks with delta.reasoning_content: {len(reasoning_parts)}")
    print(f"  Manually assembled content: {assembled!r:.200}")
    print(f"  stream_chunk_builder.content: {scb_content!r:.200}")
    print(f"{'='*60}\n")

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
    for entry in result["flag"]:
        assert isinstance(entry, dict), f"Flag entry not a dict: {entry}"
        assert "id" in entry, f"Flag entry missing 'id': {entry}"

    print(f"\n  PASS — response: flag={result['flag']}")
