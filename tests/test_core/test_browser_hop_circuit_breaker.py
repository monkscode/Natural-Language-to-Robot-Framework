# tests/test_core/test_browser_hop_circuit_breaker.py
"""Phase 3: the hop circuit breaker opens after repeated failures and recovers."""

import time
from unittest import mock

import tools.browser_use_tool as bt


def setup_function():
    bt._breaker_reset()


def test_breaker_opens_after_threshold():
    for _ in range(bt._BREAKER_THRESHOLD):
        assert bt._breaker_allow() is True
        bt._breaker_record_failure()
    assert bt._breaker_allow() is False  # open during cooldown


def test_breaker_half_opens_after_cooldown_then_closes_on_success():
    for _ in range(bt._BREAKER_THRESHOLD):
        bt._breaker_record_failure()
    assert bt._breaker_allow() is False
    with mock.patch.object(bt.time, "time", return_value=time.time() + bt._BREAKER_COOLDOWN_S + 1):
        assert bt._breaker_allow() is True   # half-open probe allowed
        bt._breaker_record_success()
    assert bt._breaker_allow() is True       # closed again
