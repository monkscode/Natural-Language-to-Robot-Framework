"""
Wire tests for W5: the per-try timeout must really cut a hung request.

A local TCP server accepts and never answers. Through the real get_llm, the
real crewai LLM.call and the real litellm HTTP handlers, a vertex and a gemini
call must give up after exactly three requests whose timeouts grow 1x/2x/4x.
Asserting on the parameter name would be satisfied by our own input — this
asserts on what reaches the socket (lesson of project_vertex_thinking_inflation).
Offline: only 127.0.0.1 is contacted.
"""

import os
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import litellm
import pytest

from src.backend.crew_ai.provider_retry import ProviderRetryPolicy, report_of

BASE_S = 0.5  # tries at 0.5 s, 1 s, 2 s


@pytest.fixture
def hung_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    accepted, conns = [], []

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            accepted.append(time.monotonic())
            conns.append(conn)  # held open, never answered

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}/v1/models/gemini-3.5-flash", accepted
    sock.close()
    for conn in conns:
        conn.close()


def _settings():
    s = MagicMock()
    s.LLM_REQUEST_TIMEOUT_S = BASE_S
    s.LLM_EMPTY_RESPONSE_MAX_RETRIES = 2
    return patch("src.backend.core.config.settings", s)


def _assert_three_growing_tries(llm, accepted):
    started = time.monotonic()
    with pytest.raises(litellm.Timeout) as raised:
        llm.call([{"role": "user", "content": "ping"}])
    elapsed = time.monotonic() - started
    assert len(accepted) == 3, f"expected one request per try, saw {len(accepted)}"
    assert report_of(raised.value).timed_out_after_s == (0.5, 1.0, 2.0)
    assert 3.4 <= elapsed < 20, f"three tries of 0.5/1/2 s took {elapsed:.2f} s"


def test_gemini_timeout_cuts_a_hung_request_on_every_try(hung_server):
    from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

    base_url, accepted = hung_server
    with _settings():
        llm = get_llm("gemini", "gemini-3.5-flash", api_key="fake-key")
        llm.api_base = base_url
        _assert_three_growing_tries(llm, accepted)


def test_vertex_timeout_cuts_a_hung_request_on_every_try(hung_server):
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

    base_url, accepted = hung_server
    with _settings(), \
         patch.dict(os.environ, {"VERTEXAI_PROJECT": "p", "VERTEXAI_LOCATION": "global"}), \
         patch.object(VertexBase, "_ensure_access_token", return_value=("fake-token", "p")):
        llm = get_llm("vertex", "gemini-3.5-flash")
        llm.api_base = base_url
        _assert_three_growing_tries(llm, accepted)


def test_local_sends_no_timeout_and_keeps_litellm_retries():
    """Ollama is never capped (owner D3): no timeout on the wire, num_retries=3."""
    from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

    real = litellm.completion
    seen = []

    def spy(*args, **kwargs):
        seen.append(dict(kwargs))
        return real(*args, **{**kwargs, "mock_response": "ok"})

    with _settings(), patch("litellm.completion", side_effect=spy):
        llm = get_llm("local", "llama3")
        assert llm.call([{"role": "user", "content": "ping"}]) == "ok"
    assert llm._provider_retry is None
    assert "timeout" not in seen[0]
    assert seen[0]["num_retries"] == 3


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_cloud_wrappers_get_the_policy_and_no_litellm_retries(provider):
    from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

    with _settings(), patch.dict(os.environ, {"GEMINI_API_KEY": "k", "VERTEXAI_PROJECT": "p",
                                             "VERTEXAI_LOCATION": "global"}):
        llm = get_llm(provider, "gemini-3.5-flash")
    assert llm._provider_retry == ProviderRetryPolicy(BASE_S)
    assert llm.additional_params["num_retries"] == 0
