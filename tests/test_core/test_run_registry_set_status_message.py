"""RunRegistry.set_status writing error_message (E5b Part B, Task 1).

set_status is the only writer of the terminal failed/error status. Until now
it could not record why. This pins the exact-write contract the plan settled
on after reversing an earlier COALESCE draft (2026-09-21): the caller's
value is stored VERBATIM, and passing None clears the column -- there is no
newest-non-NULL-wins here, unlike record_start's error_message.

That reversal matters concretely for the Generate page, which reuses one
run id across repeated Run clicks: record_start(running) -> set_status(failed,
msg) -> record_start(running) -> set_status(passed) must end with NULL, not
the stale failure reason, or a passed run would still show why it once
failed.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (set_status, record_start).
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

USER_A = {"user_id": "alice", "org_id": "org-a", "email": "a@x.com"}


@pytest.fixture
def reg():
    name = f"set_status_msg_{uuid.uuid4().hex[:8]}"
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


def _error_message(admin, run_id):
    return admin.execute(
        "SELECT error_message FROM test_runs WHERE run_id = %s",
        (run_id,)).fetchone()[0]


def test_set_status_with_a_message_writes_it(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running", robot_code="code")

    r.set_status("run-1", "failed", "element not found: css=#missing")

    assert _error_message(admin, "run-1") == "element not found: css=#missing"


def test_fail_then_pass_on_the_same_run_id_clears_the_message(reg):
    """The Generate page reuses one run id across Run clicks. A stale
    failure reason must not survive a later pass on the same row."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running", robot_code="code")
    r.set_status("run-1", "failed", "element not found: css=#missing")
    assert _error_message(admin, "run-1") == "element not found: css=#missing"

    r.record_start("run-1", USER_A, "q", "running", robot_code="code")
    r.set_status("run-1", "passed")

    assert _error_message(admin, "run-1") is None


def test_set_status_swallows_a_pool_failure_without_raising(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running", robot_code="code")
    r.close()  # pool is now unusable

    r.set_status("run-1", "failed", "boom")  # must not raise
