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
def valid_tokenizer(tmp_path):
    """Give litellm a readable cl100k_base tokenizer without touching the network.

    litellm.main._complete_vertex_ai_beta passes `encoding=_get_encoding()` to the
    provider on EVERY gemini / vertex_ai call, so the first one loads tiktoken's
    cl100k_base. tiktoken checks the bundled file's sha256 and, on a mismatch,
    downloads it from openaipublic.blob.core.windows.net (the netguard refuses
    that). litellm's Windows wheel ships that file with CRLF line endings, so the
    hash fails there; the Linux wheel (Docker image, CI) is valid and this copy
    is then byte-identical. The tokenizer is litellm's own and unavoidable, so
    the test supplies a valid copy through litellm's CUSTOM_TIKTOKEN_CACHE_DIR.
    """
    import hashlib
    from pathlib import Path

    url = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    key = hashlib.sha1(url.encode()).hexdigest()  # tiktoken's cache file name
    bundled = Path(litellm.__file__).parent / "litellm_core_utils" / "tokenizers" / key
    (tmp_path / key).write_bytes(bundled.read_bytes().replace(b"\r\n", b"\n"))
    with patch.dict(os.environ, {"CUSTOM_TIKTOKEN_CACHE_DIR": str(tmp_path)}):
        yield


@pytest.fixture
def hung_server(valid_tokenizer):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    accepted, conns, timers = [], [], []

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            accepted.append(time.monotonic())
            conns.append(conn)  # held open, never answered
            # Safety valve (Minor 8): if a wiring regression drops the timeout
            # policy but keeps num_retries=0, an untimed client would otherwise
            # wait LiteLLM's 600 s default per socket here (~20 min for this
            # file, no CI timeout-minutes / pytest-timeout). Close each
            # connection after ~15 s so that failure is fast instead of a hang.
            timer = threading.Timer(15.0, conn.close)
            timer.daemon = True
            timer.start()
            timers.append(timer)

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}/v1/models/gemini-3.5-flash", accepted
    sock.close()
    for timer in timers:
        timer.cancel()
    for conn in conns:
        conn.close()


def _settings():
    s = MagicMock()
    s.LLM_REQUEST_TIMEOUT_S = BASE_S
    s.LLM_EMPTY_RESPONSE_MAX_RETRIES = 2
    return patch("src.backend.core.config.settings", s)


def _assert_three_growing_tries(llm, accepted, run=None):
    started = time.monotonic()
    with pytest.raises(litellm.Timeout) as raised:
        (run or (lambda: llm.call([{"role": "user", "content": "ping"}])))()
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


def test_timeout_reaches_the_socket_through_a_real_crew_with_output_pydantic(hung_server):
    """The agent-executor path: crewai maps task.output_pydantic onto response_model
    and, unless the wrapper strips it, reroutes the call through instructor, which
    drops our per-try timeout (LiteLLM's 600 s default would then apply). The direct
    llm.call tests above cannot see that, so this one runs a real Crew."""
    from crewai import Agent, Crew, Task
    from pydantic import BaseModel

    from src.backend.crew_ai.cleaned_llm_wrapper import get_llm

    class Reply(BaseModel):
        code: str

    base_url, accepted = hung_server
    with _settings():
        llm = get_llm("gemini", "gemini-3.5-flash", api_key="fake-key", response_format=Reply)
        llm.api_base = base_url
        agent = Agent(role="Tester", goal="Reply with JSON.", backstory="You reply with JSON.",
                      llm=llm, verbose=False, allow_delegation=False)
        task = Task(description="Reply with a code key.", expected_output="JSON with a code key.",
                    agent=agent, output_pydantic=Reply)
        crew = Crew(agents=[agent], tasks=[task], verbose=False)
        _assert_three_growing_tries(llm, accepted, run=crew.kickoff)


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
