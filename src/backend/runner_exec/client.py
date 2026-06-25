"""FastAPI-side client for the runner-exec service (the execution hop).

Mirrors the Phase 3 browser-service breaker: a process-global, thread-safe
circuit breaker that fast-fails when the executor is unreachable, so a dead
executor degrades gracefully instead of hanging. Connect failures trip the
breaker; a slow test run (long read timeout) does NOT.

Referenced by: services/workflow_service.py, services/dryrun_service.py,
api/endpoints.py.
Depends on: core/config.py (RUNNER_EXEC_URL), requests.
"""
import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

try:
    from src.backend.core.config import settings
    _BASE_URL = settings.RUNNER_EXEC_URL
except Exception:  # pragma: no cover — config may be absent in isolated unit runs
    _BASE_URL = "http://localhost:4998"

_CONNECT_TIMEOUT_S = 5
_QUICK_READ_TIMEOUT_S = 30           # status/cleanup/ensure-image (rebuild uses the long timeout)
# The /execute call blocks for the WHOLE test. Derive the read timeout from the
# container's own wait cap so raising TEST_EXECUTION_TIMEOUT can never make the
# HTTP read time out first (which would wrongly trip the breaker). +60s margin
# lets run_test_in_container's own timeout fire and return a structured error.
_TEST_EXECUTION_TIMEOUT_S = int(os.getenv("TEST_EXECUTION_TIMEOUT", "1800"))
_EXECUTE_READ_TIMEOUT_S = _TEST_EXECUTION_TIMEOUT_S + 60
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_S = 30

_breaker_lock = threading.Lock()
_breaker_state = {"failures": 0, "opened_at": 0.0}


class RunnerExecUnavailable(RuntimeError):
    """The executor hop is unreachable (connect failure or breaker open)."""


def _breaker_reset() -> None:
    with _breaker_lock:
        _breaker_state["failures"] = 0
        _breaker_state["opened_at"] = 0.0


def _breaker_allow() -> bool:
    with _breaker_lock:
        if _breaker_state["opened_at"] == 0.0:
            return True
        if time.time() - _breaker_state["opened_at"] >= _BREAKER_COOLDOWN_S:
            return True
        return False


def _breaker_record_failure() -> None:
    with _breaker_lock:
        _breaker_state["failures"] += 1
        if _breaker_state["failures"] >= _BREAKER_THRESHOLD:
            _breaker_state["opened_at"] = time.time()


def _breaker_record_success() -> None:
    with _breaker_lock:
        _breaker_state["failures"] = 0
        _breaker_state["opened_at"] = 0.0


def _call(method: str, path: str, *, read_timeout: int, json_body: dict | None = None) -> dict:
    if not _breaker_allow():
        raise RunnerExecUnavailable("runner-exec circuit breaker open")
    url = f"{_BASE_URL}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, timeout=(_CONNECT_TIMEOUT_S, read_timeout))
        else:
            resp = requests.post(url, json=json_body, timeout=(_CONNECT_TIMEOUT_S, read_timeout))
        resp.raise_for_status()
        body = resp.json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError) as e:
        # Executor unreachable / not answering, or the connection dropped mid-body
        # (ChunkedEncodingError during resp.json()) — a transport-level failure, so
        # trip the breaker.
        _breaker_record_failure()
        raise RunnerExecUnavailable(f"runner-exec unreachable: {e}") from e
    except requests.exceptions.HTTPError as e:
        # The hop is UP (it returned a status) — do NOT trip the breaker. Surface
        # the server-side detail (run_test_in_container's message carries the
        # container stderr) so the caller shows a real error, not a bare 500.
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:  # noqa: BLE001
            detail = (e.response.text or "")[:1000]
        raise RunnerExecUnavailable(f"runner-exec error {e.response.status_code}: {detail}") from e
    except ValueError as e:
        # 200 with a malformed/empty body: the hop is UP (do NOT trip the breaker)
        # but returned garbage. Surface the documented failure type instead of
        # leaking a raw JSONDecodeError after a phantom success was recorded.
        raise RunnerExecUnavailable(f"runner-exec invalid response body: {e}") from e
    except requests.exceptions.RequestException as e:
        raise RunnerExecUnavailable(f"runner-exec error: {e}") from e
    _breaker_record_success()
    return body


def ensure_image() -> dict:
    return _call("POST", "/ensure-image", read_timeout=_QUICK_READ_TIMEOUT_S)


def execute(run_id: str, test_filename: str) -> dict:
    return _call("POST", "/execute", read_timeout=_EXECUTE_READ_TIMEOUT_S,
                 json_body={"run_id": run_id, "test_filename": test_filename})


def dryrun(run_id: str, code: str) -> dict:
    return _call("POST", "/dryrun", read_timeout=_EXECUTE_READ_TIMEOUT_S,
                 json_body={"run_id": run_id, "code": code})


def rebuild_image() -> dict:
    return _call("POST", "/rebuild-image", read_timeout=_EXECUTE_READ_TIMEOUT_S)


def docker_status() -> dict:
    return _call("GET", "/docker-status", read_timeout=_QUICK_READ_TIMEOUT_S)


def cleanup() -> dict:
    return _call("POST", "/cleanup", read_timeout=_QUICK_READ_TIMEOUT_S)
