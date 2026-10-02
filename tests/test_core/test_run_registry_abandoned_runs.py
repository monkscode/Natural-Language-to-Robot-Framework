"""RunRegistry.close_abandoned_runs: turn long-dead 'running' rows into 'error'.

A server that dies mid-run leaves its test_runs row on 'running' with nothing
alive to correct it. The sweep closes rows still 'running' after the limit, and
only those. Runs against a throwaway schema, never the live tables.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (close_abandoned_runs,
ABANDONED_RUN_MESSAGE, set_status, record_start).
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

USER_A = {"user_id": "alice", "org_id": "org-a", "email": "a@x.com"}
LIMIT_S = 3600


@pytest.fixture
def reg():
    name = f"abandoned_runs_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    admin.execute(f"SET search_path TO {name}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{name}"
    from src.backend.core.run_registry import RunRegistry
    r = RunRegistry(dsn=dsn)
    try:
        yield r, admin
    finally:
        r.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _age(admin, run_id, seconds):
    admin.execute(
        "UPDATE test_runs SET updated_at = now() - make_interval(secs => %s) "
        "WHERE run_id = %s", (seconds, run_id))


def _row(admin, run_id):
    return admin.execute(
        "SELECT status, error_message FROM test_runs WHERE run_id = %s",
        (run_id,)).fetchone()


def test_old_running_row_becomes_error_with_the_message(reg):
    from src.backend.core.run_registry import ABANDONED_RUN_MESSAGE
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running", robot_code="code")
    _age(admin, "run-1", LIMIT_S + 60)

    assert r.close_abandoned_runs(LIMIT_S) == 1

    assert _row(admin, "run-1") == ("error", ABANDONED_RUN_MESSAGE)
    assert ABANDONED_RUN_MESSAGE == (
        "This run never reported a result. The server stopped, or lost the "
        "run, before it finished.")


def test_young_running_row_is_untouched(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running", robot_code="code")
    _age(admin, "run-1", LIMIT_S - 600)

    assert r.close_abandoned_runs(LIMIT_S) == 0

    assert _row(admin, "run-1") == ("running", None)


def test_finished_rows_of_any_age_are_untouched_messages_included(reg):
    r, admin = reg
    expected = {}
    for run_id, status, message in (
        ("run-passed", "passed", None),
        ("run-failed", "failed", "element not found: css=#missing"),
        ("run-error", "error", "boom"),
        ("run-generated", "generated", None),
    ):
        r.record_start(run_id, USER_A, "q", "running", robot_code="code")
        r.set_status(run_id, status, message)
        _age(admin, run_id, LIMIT_S * 10)
        expected[run_id] = (status, message)

    assert r.close_abandoned_runs(LIMIT_S) == 0

    for run_id, want in expected.items():
        assert _row(admin, run_id) == want


def test_swept_by_mistake_then_finished_ends_passed_with_null_message(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running", robot_code="code")
    _age(admin, "run-1", LIMIT_S + 60)
    assert r.close_abandoned_runs(LIMIT_S) == 1
    assert _row(admin, "run-1")[0] == "error"

    r.set_status("run-1", "passed", None)

    assert _row(admin, "run-1") == ("passed", None)


def test_pool_failure_returns_zero_without_raising(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running", robot_code="code")
    r.close()  # pool is now unusable

    assert r.close_abandoned_runs(LIMIT_S) == 0
