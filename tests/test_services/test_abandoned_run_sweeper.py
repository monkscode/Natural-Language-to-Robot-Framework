"""sweep_abandoned_runs_forever: the periodic closer of abandoned 'running' rows.

Pins the loop's contract with asyncio.sleep and the registry patched, so
nothing really sleeps and no database is touched: it sweeps once before its
first sleep, a sweep that raises does not end it, and cancelling it ends it.

Referenced by: none (service unit tests only).
Depends on: src/backend/services/workflow_service.py
(sweep_abandoned_runs_forever, _ABANDONED_AFTER_S, _SWEEP_EVERY_S).
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services import workflow_service as ws


def _run(coro):
    return asyncio.run(coro)


def test_sweeps_once_before_its_first_sleep_with_the_limit():
    registry = MagicMock()
    registry.close_abandoned_runs.return_value = []
    order = []
    registry.close_abandoned_runs.side_effect = (
        lambda limit: order.append(("sweep", limit)) or [])

    async def fake_sleep(seconds):
        order.append(("sleep", seconds))
        raise asyncio.CancelledError

    async def go():
        with patch.object(ws, "get_run_registry", return_value=registry), \
                patch.object(ws.asyncio, "sleep", fake_sleep):
            await ws.sweep_abandoned_runs_forever()

    with pytest.raises(asyncio.CancelledError):
        _run(go())

    assert order == [("sweep", ws._ABANDONED_AFTER_S),
                     ("sleep", ws._SWEEP_EVERY_S)]
    assert ws._ABANDONED_AFTER_S == max(
        3600,
        ws.runner_exec_client._IMAGE_PROVISION_READ_TIMEOUT_S
        + ws.runner_exec_client._EXECUTE_READ_TIMEOUT_S + ws._SWEEP_EVERY_S)
    assert ws._ABANDONED_AFTER_S >= 3600
    assert ws._SWEEP_EVERY_S == 600


def test_a_sweep_that_raises_does_not_end_the_loop():
    registry = MagicMock()
    registry.close_abandoned_runs.side_effect = [RuntimeError("db down"), ["run-x", "run-y"], []]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    async def go():
        with patch.object(ws, "get_run_registry", return_value=registry), \
                patch.object(ws.asyncio, "sleep", fake_sleep):
            await ws.sweep_abandoned_runs_forever()

    with pytest.raises(asyncio.CancelledError):
        _run(go())

    assert registry.close_abandoned_runs.call_count == 3
    assert len(sleeps) == 3


def test_cancelling_the_task_ends_it():
    registry = MagicMock()
    registry.close_abandoned_runs.return_value = []
    parked = None

    async def fake_sleep(seconds):
        parked.set()
        await asyncio.Event().wait()  # park until cancelled

    async def go():
        nonlocal parked
        parked = asyncio.Event()
        with patch.object(ws, "get_run_registry", return_value=registry), \
                patch.object(ws.asyncio, "sleep", fake_sleep):
            task = asyncio.create_task(ws.sweep_abandoned_runs_forever())
            await parked.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return task

    task = _run(go())
    assert task.cancelled()


def test_the_warning_names_the_closed_run_ids(caplog):
    registry = MagicMock()
    registry.close_abandoned_runs.return_value = ["run-x", "run-y"]

    async def fake_sleep(seconds):
        raise asyncio.CancelledError

    async def go():
        with patch.object(ws, "get_run_registry", return_value=registry), \
                patch.object(ws.asyncio, "sleep", fake_sleep):
            await ws.sweep_abandoned_runs_forever()

    with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
        _run(go())

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "run-x" in warnings[0] and "run-y" in warnings[0]
