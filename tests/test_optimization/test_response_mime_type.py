"""
#14 verification — does `response_format={"type":"json_object"}` work with
the existing Gemini-via-Vertex AI streaming setup?

Goal: prove that adding response_format eliminates the ```json fences
the model currently wraps responses in, without breaking the thinking-mode
delta.content fallback that test_conflict_detection_live.py relies on.

Run:
    venv/Scripts/python.exe -m pytest tests/test_optimization/test_response_mime_type.py -v -s -m integration

If PASS:  it's safe to add response_format to extra_kwargs in
          learning_config._call_conflict_detection_llm.
If FAIL:  parser tolerance already handles fences — skip the change.
"""

import json
import pytest

from tests.test_optimization.test_conflict_detection_live import (
    _FAILED_CODE,
    _WORKING_CODE,
    _ACTIVE_HINTS,
    _parse_conflict_json,
)


def _prompt():
    from src.backend.services.workflow_service import _build_conflict_prompt
    return _build_conflict_prompt(_FAILED_CODE, _WORKING_CODE, _ACTIVE_HINTS)


@pytest.mark.integration
def test_response_format_eliminates_fences():
    """Same LLM call as baseline, but with response_format added.

    Expectations:
      1. Streaming still works (no crash).
      2. Content is returned non-empty.
      3. Content does NOT begin with ```json fences (the whole point).
      4. Content parses as valid JSON.
    """
    import litellm
    from src.backend.crew_ai.optimization.learning_config import (
        _get_conflict_detection_model,
        _get_conflict_detection_completion_kwargs,
    )

    model_string = _get_conflict_detection_model()
    extra_kwargs = _get_conflict_detection_completion_kwargs()
    # The #14 change under test:
    extra_kwargs = {**extra_kwargs, "response_format": {"type": "json_object"}}

    prompt = _prompt()
    chunks = []
    for chunk in litellm.completion(
        model=model_string,
        messages=[{"role": "user", "content": prompt}],
        timeout=90,
        stream=True,
        **extra_kwargs,
    ):
        chunks.append(chunk)

    response = litellm.stream_chunk_builder(chunks, messages=[{"role": "user", "content": prompt}])
    assert response is not None, "stream_chunk_builder returned None"

    content = response.choices[0].message.content
    if not content:
        # Apply the same delta.content fallback the framework uses
        content = "".join(
            (c.choices[0].delta.content or "") for c in chunks if c.choices
        )

    print(f"\n--- RESPONSE WITH response_format ---")
    print(f"Length: {len(content) if content else 0}")
    print(f"First 200 chars: {content[:200] if content else None!r}")
    print(f"Starts with backtick fence? {bool(content and content.lstrip().startswith('```'))}")

    assert content, "Content empty even with response_format set"

    # The core assertion: no markdown fences
    starts_clean = content.lstrip().startswith("{")
    assert starts_clean, (
        f"Content still wrapped in fences/prose even with response_format. "
        f"First 50 chars: {content[:50]!r}"
    )

    # And the JSON itself must parse
    result = _parse_conflict_json(content)
    assert "flag" in result and isinstance(result["flag"], list)
    print(f"PASS — flag={result['flag']}, reason snippet={str(result.get('reason',''))[:80]!r}")
