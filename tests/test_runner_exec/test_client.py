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
