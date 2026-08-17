from unittest.mock import patch, MagicMock
import requests
import pytest
from src.backend.runner_exec import client as rc


@pytest.fixture(autouse=True)
def _reset_breaker():
    rc._breaker_reset()
    yield
    rc._breaker_reset()


def _resp(json_body, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_body
    m.raise_for_status.return_value = None
    return m


def test_execute_returns_body_on_success():
    with patch.object(rc.requests, "post", return_value=_resp({"test_status": "passed"})) as p:
        out = rc.execute("abc123", "test.robot")
    assert out["test_status"] == "passed"
    # long read timeout for the test-execution call (connect, read) tuple
    _, kwargs = p.call_args
    assert kwargs["timeout"][1] >= rc._EXECUTE_READ_TIMEOUT_S


def test_breaker_opens_after_threshold_then_fast_fails():
    with patch.object(rc.requests, "post", side_effect=requests.exceptions.ConnectionError("down")):
        for _ in range(rc._BREAKER_THRESHOLD):
            with pytest.raises(rc.RunnerExecUnavailable):
                rc.execute("abc123", "test.robot")
    # breaker now open — next call fast-fails WITHOUT hitting requests
    with patch.object(rc.requests, "post", side_effect=AssertionError("should not be called")):
        with pytest.raises(rc.RunnerExecUnavailable):
            rc.execute("abc123", "test.robot")


def test_breaker_recovers_after_cooldown():
    with patch.object(rc.requests, "post", side_effect=requests.exceptions.ConnectionError("down")):
        for _ in range(rc._BREAKER_THRESHOLD):
            with pytest.raises(rc.RunnerExecUnavailable):
                rc.execute("abc123", "test.robot")
    with patch.object(rc, "_BREAKER_COOLDOWN_S", 0), \
         patch.object(rc.requests, "post", return_value=_resp({"test_status": "passed"})):
        out = rc.execute("abc123", "test.robot")
    assert out["test_status"] == "passed"


def test_mid_body_connection_drop_trips_breaker():
    # A connection that drops while the body is being read (ChunkedEncodingError
    # during resp.json()) is a transport-level failure: it must trip the breaker
    # like a connect failure, not be treated as a hop-is-up error.
    with patch.object(rc.requests, "post",
                      side_effect=requests.exceptions.ChunkedEncodingError("peer reset")):
        for _ in range(rc._BREAKER_THRESHOLD):
            with pytest.raises(rc.RunnerExecUnavailable):
                rc.execute("abc123", "test.robot")
    # breaker now open — next call fast-fails WITHOUT hitting requests
    with patch.object(rc.requests, "post", side_effect=AssertionError("should not be called")):
        with pytest.raises(rc.RunnerExecUnavailable):
            rc.execute("abc123", "test.robot")


def test_malformed_success_body_surfaces_as_unavailable():
    # A 200 whose body will not parse: the hop is UP but returned garbage. It must
    # surface as RunnerExecUnavailable (the documented failure type), not leak a raw
    # JSONDecodeError, and must not record a phantom success before the parse.
    bad = MagicMock()
    bad.status_code = 200
    bad.raise_for_status.return_value = None
    bad.json.side_effect = ValueError("Expecting value")
    with patch.object(rc.requests, "post", return_value=bad):
        with pytest.raises(rc.RunnerExecUnavailable):
            rc.execute("abc123", "test.robot")


def test_ensure_image_allows_time_for_a_cold_image_pull():
    # ensure-image is the FIRST-RUN path: on a machine that has never run a test
    # the runner image is absent and this call downloads ~1.9 GB. Measured cold:
    # ~94s even with most layers already local. The 30s quick timeout made every
    # new user's first "Run Test" fail with "runner-exec unreachable", which reads
    # as a network fault rather than a download in progress. It must get the same
    # generous budget as the rebuild path, which does the same kind of work.
    with patch.object(rc.requests, "post", return_value=_resp({"status": "ready"})) as p:
        rc.ensure_image()
    _, kwargs = p.call_args
    assert kwargs["timeout"][1] >= rc._IMAGE_PROVISION_READ_TIMEOUT_S
    assert rc._IMAGE_PROVISION_READ_TIMEOUT_S > rc._QUICK_READ_TIMEOUT_S


def test_malformed_timeout_env_falls_back_without_raising(monkeypatch):
    # A non-integer TEST_EXECUTION_TIMEOUT must not raise at import time:
    # runner_exec.client is imported by workflow_service/dryrun_service/endpoints,
    # so a bad value would take down the whole API. It falls back to 1800s.
    import importlib

    monkeypatch.setenv("TEST_EXECUTION_TIMEOUT", "not-an-int")
    try:
        reloaded = importlib.reload(rc)
        assert reloaded._TEST_EXECUTION_TIMEOUT_S == 1800
        assert reloaded._EXECUTE_READ_TIMEOUT_S == 1860
    finally:
        # Restore the module to its default-env state for the rest of the suite.
        monkeypatch.delenv("TEST_EXECUTION_TIMEOUT", raising=False)
        importlib.reload(rc)
