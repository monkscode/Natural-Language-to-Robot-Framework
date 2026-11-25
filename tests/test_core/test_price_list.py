"""The LiteLLM price list: the shipped copy at import, the live one right after FastAPI starts.

litellm 1.105.3 downloads its price list during `import litellm`; a failed first try starts a
retry thread while the import is still running, and the two could crash the import. Both
server entry modules set LITELLM_LOCAL_MODEL_COST_MAP=True first (no download, no thread),
and FastAPI's startup then downloads the live list with litellm's own refetch + adopt.

No test here reaches the network: the job's refetch is replaced, and the child processes
point litellm at a refused loopback port and refuse every other lookup and connect.

Referenced by: none (leaf test module).
Depends on: src/backend/core/price_list.py, src/backend/main.py, src/backend/runner_exec/app.py,
src/backend/crew_ai/model_map_fallback.py, litellm 1.105.3.
"""

import ast
import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import litellm
import pytest
from litellm.litellm_core_utils.get_model_cost_map import (
    ModelCostMapReloaded,
    ModelCostMapReloadUnavailable,
)

from src.backend.core import price_list
from src.backend.core.config import settings
from src.backend.crew_ai import model_map_fallback as fallback

VAR = "LITELLM_LOCAL_MODEL_COST_MAP"
REPO_ROOT = Path(__file__).resolve().parents[2]
COST_MAP = "litellm.litellm_core_utils.get_model_cost_map"
RELOADED = ModelCostMapReloaded(model_cost_map={"vertex_ai/gemini-3.8-flash": {}})
UNAVAILABLE = ModelCostMapReloadUnavailable(reason="model cost map fetch failed")


# --- at import: the shipped list, unless the operator decided ---

def test_an_absent_variable_is_set_and_reported_as_ours():
    with patch.dict(os.environ):
        os.environ.pop(VAR, None)
        assert price_list.use_shipped_price_list() is True
        assert os.environ[VAR] == "True"


@pytest.mark.parametrize("value", ["True", "False"])
def test_an_operator_value_is_untouched_and_not_ours(value):
    with patch.dict(os.environ, {VAR: value}):
        assert price_list.use_shipped_price_list() is False
        assert os.environ[VAR] == value


def test_the_session_pin_keeps_every_startup_from_downloading():
    """tests/conftest.py pins the variable, so to main it is an operator value: no test that runs
    main's startup starts a download, and no test-process import of litellm tries GitHub."""
    from src.backend import main

    assert os.environ.get(VAR) == "True"
    assert main._PRICE_LIST_SET_BY_US is False


def test_main_sets_the_variable_before_any_other_import():
    """main.py imports the app's whole chain (and litellm with it); the call must come first."""
    body = ast.parse((REPO_ROOT / "src/backend/main.py").read_text(encoding="utf-8")).body
    assert [ast.unparse(node) for node in body[:3]] == [
        "import os",
        "from src.backend.core.price_list import use_shipped_price_list",
        "_PRICE_LIST_SET_BY_US = use_shipped_price_list()",
    ]


# A child that cannot leave the machine (only loopback resolves or connects), records the name of
# every thread started, imports runner-exec's app, and prints what it saw.
_CHILD = textwrap.dedent("""
    import json, socket, threading
    LOOPBACK = ("127.0.0.1", "::1", "localhost")
    _gai, _connect = socket.getaddrinfo, socket.socket.connect
    def gai(host, *a, **k):
        if (host.decode() if isinstance(host, bytes) else host) not in LOOPBACK:
            raise socket.gaierror(socket.EAI_NONAME, "refused by the test")
        return _gai(host, *a, **k)
    def connect(self, addr):
        if not (isinstance(addr, tuple) and addr[0] in LOOPBACK):
            raise ConnectionRefusedError("refused by the test")
        return _connect(self, addr)
    socket.getaddrinfo, socket.socket.connect = gai, connect
    started = []
    _start = threading.Thread.start
    def start(self):
        started.append(self.name)
        return _start(self)
    threading.Thread.start = start
    error = None
    try:
        import src.backend.runner_exec.app  # noqa: F401
    except Exception as e:
        error = repr(e)
    print(json.dumps({"started": started, "error": error}), flush=True)
""")


def _import_runner_exec_in_child(tmp_path, local_map: str | None):
    # The session env without our variable (conftest pins it) and without proxies; litellm's
    # download goes to a refused loopback port: a retryable transport failure, no real network.
    env = {k: v for k, v in os.environ.items() if k != VAR and not k.lower().endswith("_proxy")}
    env["no_proxy"] = "*"
    env["LITELLM_MODEL_COST_MAP_URL"] = "http://127.0.0.1:9/model_prices.json"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    if local_map is not None:
        env[VAR] = local_map
    # cwd outside the repo: config.py's load_dotenv("src/backend/.env") must not find the real one
    result = subprocess.run([sys.executable, "-c", _CHILD], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_runner_exec_starts_no_download_thread(tmp_path):
    """Recorded at start, not at exit: litellm's retry thread gives up after ~6 s, possibly
    before the import ends."""
    seen = _import_runner_exec_in_child(tmp_path, local_map=None)
    assert seen["error"] is None
    assert "litellm-model-cost-map-retry" not in seen["started"]


def test_the_control_without_the_variable_does_start_it(tmp_path):
    """Proves the test above can see the thread: an operator's False keeps litellm's own
    download at import, which fails on the refused port and starts the retry thread."""
    seen = _import_runner_exec_in_child(tmp_path, local_map="False")
    assert "litellm-model-cost-map-retry" in seen["started"]


# --- after start: the live list (FastAPI only) ---

@contextmanager
def _fakes(refetch_side_effect, events=None):
    """Replaces litellm's refetch and adopt and the model check; nothing here touches the network."""
    events = [] if events is None else events

    async def refetch(url):
        events.append("fetch")
        outcome = refetch_side_effect.pop(0) if isinstance(refetch_side_effect, list) else refetch_side_effect
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fakes = SimpleNamespace(refetch=AsyncMock(side_effect=refetch), adopt=MagicMock(return_value=7170),
                            ensure=MagicMock(), checked={"vertex_ai/gemini-2.5-flash"}, events=events)
    with patch(f"{COST_MAP}.refetch_model_cost_map", fakes.refetch), \
         patch(f"{COST_MAP}.adopt_model_cost_map", fakes.adopt), \
         patch.object(fallback, "ensure_model_entry", fakes.ensure), \
         patch.object(fallback, "_checked", fakes.checked), \
         patch.object(settings, "MODEL_PROVIDER", "vertex"), \
         patch.object(settings, "ONLINE_MODEL", "gemini-3.8-flash"):
        yield fakes


def _sleep(events):
    return AsyncMock(side_effect=lambda seconds: events.append("wait"))


def test_success_on_the_first_round_adopts_once_and_rechecks(caplog):
    seen_var = []

    async def go():
        await price_list.start_live_price_list(True)

    with patch.dict(os.environ, {VAR: "True"}), _fakes(RELOADED) as f, \
         caplog.at_level(logging.INFO, logger=price_list.__name__):
        f.refetch.side_effect = lambda url: seen_var.append(os.environ.get(VAR)) or RELOADED
        asyncio.run(go())
        assert VAR not in os.environ
    assert seen_var == [None]  # litellm's refetch obeys the variable too: ours is gone before it runs
    f.refetch.assert_awaited_once_with(url=litellm.model_cost_map_url)
    f.adopt.assert_called_once_with(RELOADED.model_cost_map)
    assert f.checked == set()  # every remembered model is checked again on its next use
    f.ensure.assert_called_once_with("vertex", "vertex_ai/gemini-3.8-flash")
    assert [r.levelname for r in caplog.records] == ["INFO"]
    assert "7170" in caplog.records[0].getMessage()


def test_seven_failures_are_seven_rounds_ten_minutes_apart_then_one_warning(caplog):
    events = []
    sleep = _sleep(events)
    with _fakes(UNAVAILABLE, events) as f, caplog.at_level(logging.INFO, logger=price_list.__name__):
        asyncio.run(price_list.load_live_price_list(sleep=sleep))
    assert events == ["fetch"] + ["wait", "fetch"] * 6  # one round at start, then every 10 min for 1 h
    assert sleep.await_args_list == [call(600)] * 6
    f.adopt.assert_not_called()
    f.ensure.assert_not_called()
    assert f.checked == {"vertex_ai/gemini-2.5-flash"}
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "until the next restart" in warnings[0].getMessage()


@pytest.mark.parametrize("failure", [UNAVAILABLE, RuntimeError("boom")], ids=["unavailable", "exception"])
def test_a_failure_then_a_success_adopts_once_and_stops(caplog, failure):
    events = []
    sleep = _sleep(events)
    with _fakes([failure, RELOADED], events) as f, caplog.at_level(logging.INFO, logger=price_list.__name__):
        asyncio.run(price_list.load_live_price_list(sleep=sleep))
    assert events == ["fetch", "wait", "fetch"]
    sleep.assert_awaited_once_with(600)
    f.adopt.assert_called_once_with(RELOADED.model_cost_map)
    f.ensure.assert_called_once_with("vertex", "vertex_ai/gemini-3.8-flash")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_operator_value_starts_no_job():
    with patch.dict(os.environ, {VAR: "False"}):
        assert price_list.start_live_price_list(False) is None
        assert os.environ[VAR] == "False"


@pytest.mark.parametrize("set_by_us", [True, False])
def test_startup_starts_the_job_only_when_main_set_the_variable(set_by_us):
    from src.backend import main

    async def job():
        pass

    async def go():
        with pytest.raises(RuntimeError, match="stop here"):
            await main.startup_event()
        return getattr(main.app.state, "live_price_list", None)

    with patch.dict(os.environ, {VAR: "True"}), \
         patch.object(main, "_PRICE_LIST_SET_BY_US", set_by_us), \
         patch.object(price_list, "load_live_price_list", job), \
         patch("src.backend.auth.security_posture.validate_security_posture"), \
         patch("src.backend.core.artifact_store.get_artifact_store"), \
         patch.object(fallback, "ensure_model_entry"), \
         patch.object(main, "_check_learning_health", side_effect=RuntimeError("stop here")):
        task = asyncio.run(go())
        assert (VAR in os.environ) is not set_by_us
    assert isinstance(task, asyncio.Task) is set_by_us
