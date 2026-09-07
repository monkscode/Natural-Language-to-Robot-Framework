"""record_start attaches a run to its test (owner decisions D6, D7, D8).

record_start is an UPSERT called several times per run — at generation start
(no code), at generation success (code), and again at execute. Attachment must
therefore be idempotent on run_id, or a single run would spawn several tests.
"""
import uuid
from unittest.mock import patch

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

USER_A = {"user_id": "alice", "org_id": "org-a", "email": "a@x.com"}


@pytest.fixture
def reg():
    name = f"tests_attach_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    # The registry writes into {name}; without this the admin connection still
    # resolves an unqualified `test_runs` against public and every assertion
    # reads a different table (or UndefinedTable, if nothing built one there).
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


def _row(admin, run_id):
    return admin.execute(
        "SELECT test_id, test_version_id, org_id, ran_as_platform_admin "
        "FROM test_runs WHERE run_id = %s", (run_id,)).fetchone()


def test_a_generation_with_code_creates_its_test_and_first_version(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "search shoes", "generated",
                   robot_code="*** Tasks ***")

    test_id, version_id, org_id, _ = _row(admin, "run-1")
    assert test_id is not None
    assert version_id is not None
    assert org_id == "org-a"
    assert admin.execute(
        "SELECT n, robot_code FROM test_versions WHERE version_id = %s",
        (version_id,)).fetchone() == (1, "*** Tasks ***")
    assert admin.execute(
        "SELECT key_n, user_query FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == (1, "search shoes")


def test_a_failed_generation_creates_no_test(reg):
    """Owner decision D8 scenario (a): nothing useful was produced, so nothing
    is stored beyond the run row itself."""
    r, admin = reg
    r.record_start("run-1", USER_A, "search shoes", "error",
                   error_message="planner failed")

    test_id, version_id, _, _ = _row(admin, "run-1")
    assert test_id is None
    assert version_id is None
    assert admin.execute("SELECT count(*) FROM tests").fetchone()[0] == 0


def test_attachment_is_idempotent_across_the_upserts_of_one_run(reg):
    """record_start runs at generation start, at generation success and at
    execute. One run must still be one test."""
    r, admin = reg
    r.record_start("run-1", USER_A, "search shoes", "running")
    r.record_start("run-1", USER_A, "search shoes", "generated",
                   robot_code="code-1")
    r.record_start("run-1", USER_A, "search shoes", "running",
                   robot_code="code-1")

    assert admin.execute("SELECT count(*) FROM tests").fetchone()[0] == 1
    assert admin.execute(
        "SELECT count(*) FROM test_versions").fetchone()[0] == 1


def test_an_edited_code_execution_does_not_mint_a_second_version(reg):
    """P1 is the spine only. Versioning on edit is P2's Update dialog; doing it
    here would inflate every version count on day one."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated", robot_code="code-1")
    r.record_start("run-1", USER_A, "q", "running", robot_code="code-EDITED")

    assert admin.execute(
        "SELECT count(*) FROM test_versions").fetchone()[0] == 1
    # test_runs.robot_code still follows newest-non-NULL-wins, unchanged.
    assert admin.execute(
        "SELECT robot_code FROM test_runs WHERE run_id = 'run-1'"
    ).fetchone()[0] == "code-EDITED"


def test_a_rerun_joins_its_source_test_and_adds_no_version(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated", robot_code="code-1")
    source_test = _row(admin, "run-1")[0]

    r.record_start("run-2", USER_A, "q", "running",
                   robot_code="code-1", rerun_of="run-1")

    test_id, version_id, _, _ = _row(admin, "run-2")
    assert test_id == source_test
    assert admin.execute("SELECT count(*) FROM tests").fetchone()[0] == 1
    assert admin.execute(
        "SELECT count(*) FROM test_versions").fetchone()[0] == 1
    assert version_id == admin.execute(
        "SELECT version_id FROM test_versions WHERE test_id = %s AND n = 1",
        (test_id,)).fetchone()[0]


def test_a_cross_org_rerun_takes_the_org_of_the_test_not_the_caller(reg):
    """Owner decision D6. A platform admin re-running another org's test writes
    a result in the TEST's org, so tests.org_id = test_runs.org_id is an
    invariant. It also stops the admin's own org's learning store from
    receiving another org's signal."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated", robot_code="code-1")

    admin_user = {"user_id": "root", "org_id": "org-ADMIN", "email": "r@x.com"}
    r.record_start("run-2", admin_user, "q", "running",
                   robot_code="code-1", rerun_of="run-1",
                   is_platform_admin=True)

    test_id, _, org_id, flag = _row(admin, "run-2")
    assert org_id == "org-a"          # the TEST's org, not org-ADMIN
    assert flag is True               # D7
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s", (test_id,)
    ).fetchone()[0] == "org-a"


def test_the_admin_flag_is_recorded_on_every_admin_run_not_only_cross_org(reg):
    """One rule, no special case — it reads as an audit trail rather than a
    badge that appears only when something odd happened."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated",
                   robot_code="c", is_platform_admin=True)
    assert _row(admin, "run-1")[3] is True


def test_an_ordinary_run_is_not_flagged(reg):
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated", robot_code="c")
    assert _row(admin, "run-1")[3] is False


def test_a_token_less_run_still_gets_a_test(reg):
    """The bench and AUTH_ENFORCED=false. org_id is NULL, which is exactly why
    tests.org_id is nullable."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="c")

    test_id, _, org_id, _ = _row(admin, "run-1")
    assert test_id is not None
    assert org_id is None
    assert admin.execute(
        "SELECT org_id, key_n FROM tests").fetchone() == (None, 1)


def test_a_test_failure_never_breaks_the_run_row(reg):
    """Bookkeeping may never take down the pipeline. If the test write fails,
    the run row must still exist."""
    r, admin = reg
    with patch.object(type(r), "_attach_test",
                      side_effect=RuntimeError("boom")):
        r.record_start("run-1", USER_A, "q", "generated", robot_code="c")

    assert admin.execute(
        "SELECT count(*) FROM test_runs WHERE run_id = 'run-1'"
    ).fetchone()[0] == 1
    assert _row(admin, "run-1")[0] is None


class _CollidesOnceOnTestInsert:
    """A connection wrapper whose FIRST `INSERT INTO tests` raises
    UniqueViolation, exactly as Postgres does when two record_start calls in
    one org race: both read `coalesce(max(key_n), 0) + 1` as the same number
    and the loser hits idx_tests_org_key.

    A genuine two-connection race is not reproducible on demand — the loser
    only exists if both transactions overlap the same instant — so the
    collision is injected instead. Everything after it is the real code path:
    the real savepoint, the real recompute, the real second INSERT.
    """

    def __init__(self, real):
        self._real = real
        self.collisions = 0

    def execute(self, sql, params=None):
        if "INSERT INTO tests" in sql and self.collisions == 0:
            self.collisions += 1
            raise psycopg.errors.UniqueViolation(
                'duplicate key value violates unique constraint'
                ' "idx_tests_org_key"')
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_key_collision_is_retried_rather_than_losing_the_test(reg, caplog):
    """The key allocation reads max(key_n) and writes max+1 in one statement,
    which is still racy across two transactions: the loser raises
    UniqueViolation, record_start's outer except swallows it, and the run is
    silently left unattached. One bounded retry recomputes the key."""
    import logging

    r, admin = reg
    real_attach = type(r)._attach_test
    seen = {}

    def _through_a_colliding_connection(self, conn, *args, **kwargs):
        wrapper = seen.setdefault("conn", _CollidesOnceOnTestInsert(conn))
        return real_attach(self, wrapper, *args, **kwargs)

    with caplog.at_level(logging.WARNING, logger="src.backend.core.run_registry"), \
            patch.object(type(r), "_attach_test",
                         _through_a_colliding_connection):
        r.record_start("run-1", USER_A, "q", "generated", robot_code="c")

    assert seen["conn"].collisions == 1
    test_id, version_id, org_id, _ = _row(admin, "run-1")
    assert test_id is not None
    assert version_id is not None
    assert admin.execute(
        "SELECT key_n FROM tests WHERE test_id = %s", (test_id,)
    ).fetchone()[0] == 1
    assert any("key collision" in m for m in caplog.messages)


def test_a_second_key_collision_gives_up_without_losing_the_run(reg):
    """The retry is bounded at one. A second collision leaves the run
    unattached — which D8 already makes a legal state — rather than spinning."""
    r, admin = reg

    class _AlwaysCollides(_CollidesOnceOnTestInsert):
        def execute(self, sql, params=None):
            if "INSERT INTO tests" in sql:
                self.collisions += 1
                raise psycopg.errors.UniqueViolation("duplicate key")
            return self._real.execute(sql, params)

    real_attach = type(r)._attach_test
    seen = {}

    def _through_a_colliding_connection(self, conn, *args, **kwargs):
        wrapper = seen.setdefault("conn", _AlwaysCollides(conn))
        return real_attach(self, wrapper, *args, **kwargs)

    with patch.object(type(r), "_attach_test",
                      _through_a_colliding_connection):
        r.record_start("run-1", USER_A, "q", "generated", robot_code="c")

    assert seen["conn"].collisions == 2
    assert admin.execute(
        "SELECT count(*) FROM test_runs WHERE run_id = 'run-1'"
    ).fetchone()[0] == 1
    assert _row(admin, "run-1")[0] is None
    assert admin.execute("SELECT count(*) FROM tests").fetchone()[0] == 0
