"""
Tests for CleanedLLMWrapper.get_context_window_size (crewai 1.15.44 upgrade, Task 4).

Owner decision: crewai's own catalog serves cloud models; only the Ollama
/api/show lookup stays ours. context_window_size holds the FULL window and
crewai applies its 85% ratio on read. Real wrappers via get_llm (as production
builds them); the only mock is requests.post, so no network is touched.
"""

from unittest.mock import MagicMock, patch

import pytest

from crewai.llm import LLM

from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

GEMINI_38_WINDOW = 891_289  # crewai catalog value (already x0.85) for gemini-3.8-flash


@pytest.mark.parametrize("provider", ["vertex", "gemini"])
def test_cloud_models_get_crewais_catalog_window(provider):
    llm = get_llm(provider, "gemini-3.8-flash", api_key="unused")
    with patch("requests.post") as post:
        assert llm.get_context_window_size() == GEMINI_38_WINDOW
    post.assert_not_called()


def _show_response(status=200, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    return resp


def test_ollama_stores_full_window_and_returns_85_percent():
    llm = get_llm("local", "llama3")
    body = {"model_info": {"llama.context_length": 8192}}
    with patch("requests.post", return_value=_show_response(body=body)) as post:
        assert llm.get_context_window_size() == int(8192 * 0.85)
        assert llm.context_window_size == 8192
        # second call is served from the stored value, no second HTTP call
        assert llm.get_context_window_size() == int(8192 * 0.85)
    assert post.call_count == 1


def test_ollama_unreachable_returns_crewai_default_and_never_raises():
    llm = get_llm("local", "llama3")
    default = LLM.get_context_window_size(llm)
    with patch("requests.post", side_effect=ConnectionError("down")):
        assert llm.get_context_window_size() == default
    assert llm.context_window_size is None  # crewai's reduced default is not stored


@pytest.mark.parametrize(
    "response",
    [_show_response(status=500), _show_response(body={"model_info": {}})],
    ids=["http-500", "no-context-length-key"],
)
def test_ollama_bad_answers_fall_back_without_storing(response):
    llm = get_llm("local", "llama3")
    with patch("requests.post", return_value=response):
        assert llm.get_context_window_size() > 0
    assert llm.context_window_size is None


def test_ollama_down_is_queried_once_per_instance():
    llm = get_llm("local", "llama3")
    with patch("requests.post", side_effect=ConnectionError("down")) as post:
        first = llm.get_context_window_size()
        second = llm.get_context_window_size()
    assert first == second
    assert post.call_count == 1
