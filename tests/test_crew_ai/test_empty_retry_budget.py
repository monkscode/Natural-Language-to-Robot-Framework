"""
Blank-answer request budget (owner decision, crewai 1.15.44 upgrade).

crewai 1.15.44's agent loop retries a blank answer once itself and the task is run
up to 3 times, so with our wrapper's own retry an all-blank Crew.kickoff() sent
3 x crewai 2 x ours. Default 2 extra asks gave 18 requests; default 1 gives 12.
The salvage path calls the wrapper directly (no crew) and gets 1 + 1 = 2.

Counts litellm.completion calls through the real get_llm wrapper and a real
Crew.kickoff(); the mock returns a real ModelResponse with content "" (a falsy
mock object would not be a mock, see memory "Falsy mock = real Vertex call").
"""

import os
from unittest.mock import MagicMock, patch

import litellm
import pytest
from crewai import Agent, Crew, Task

from src.backend.core.config import Settings


def _default() -> int:
    return Settings.model_fields["LLM_EMPTY_RESPONSE_MAX_RETRIES"].default


def _settings():
    s = MagicMock()
    s.LLM_REQUEST_TIMEOUT_S = 60
    s.LLM_EMPTY_RESPONSE_MAX_RETRIES = _default()
    return patch("src.backend.core.config.settings", s)


def _blank(*args, **kwargs):
    return litellm.ModelResponse(
        choices=[litellm.Choices(message=litellm.Message(role="assistant", content=""),
                                 finish_reason="stop")],
        usage=litellm.Usage(prompt_tokens=1, completion_tokens=0, total_tokens=1),
    )


def _llm():
    from src.backend.crew_ai.cleaned_llm_wrapper import get_llm
    with patch.dict(os.environ, {"VERTEXAI_PROJECT": "p", "VERTEXAI_LOCATION": "global"}):
        return get_llm("vertex", "gemini-3.5-flash")


def test_default_is_one_extra_ask():
    assert _default() == 1


def test_direct_call_with_always_blank_sends_two_requests():
    """The salvage path: no crewai executor around it, so only our retry applies."""
    llm = _llm()
    with _settings(), patch("time.sleep"), \
         patch("litellm.completion", side_effect=_blank) as completion:
        assert llm.call([{"role": "user", "content": "hi"}]) == ""
    assert completion.call_count == 2


def test_all_blank_crew_kickoff_sends_twelve_requests_and_fails_loudly():
    llm = _llm()
    agent = Agent(role="Planner", goal="Plan steps.", backstory="You plan steps.",
                  llm=llm, verbose=False, allow_delegation=False)
    task = Task(description="Plan the steps.", expected_output="A step list.", agent=agent)
    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    with _settings(), patch("time.sleep"), \
         patch("litellm.completion", side_effect=_blank) as completion:
        with pytest.raises(Exception) as raised:
            crew.kickoff()
    assert type(raised.value).__name__ == "EmptyLLMResponseError"
    assert completion.call_count == 12
