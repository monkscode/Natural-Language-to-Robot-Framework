import time
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
