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
                      side_effect=RuntimeError("boom")) as attach:
        r.record_start("run-1", USER_A, "q", "generated", robot_code="c")

    # Without this the test passes even if attachment were never wired at
    # all: a NULL test_id and a surviving run row are also what an
    # unattached record_start produces.
    assert attach.called, "record_start never attempted the attach"
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


def test_the_caller_org_fills_in_when_the_reruns_source_test_has_none(reg):
    """Spec section 10 case 35, rerun branch. A test can carry org_id NULL —
    the collapse mints one for every org-less row, and _lookup_org_id
    swallowing a failure writes one on the upsert that creates the test. D6
    would then hand that NULL to every later run of it, and the run would drop
    out of org-scoped History permanently."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    source_test = _row(admin, "run-1")[0]
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (source_test,)).fetchone()[0] is None

    r.record_start("run-2", USER_A, "q", "running",
                   robot_code="code-1", rerun_of="run-1")

    test_id, _, org_id, _ = _row(admin, "run-2")
    assert test_id == source_test          # D6 still binds the run to the test
    assert org_id == "org-a"               # ...but a NULL org cannot win


def test_the_caller_org_fills_in_when_the_rows_own_test_has_none(reg):
    """Same rule, the idempotent branch — the second and third upsert of one
    run read the test's org back rather than the caller's, so the NULL has to
    be caught on that path too or the run is org-less from its second write
    onward."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    assert _row(admin, "run-1")[2] is None

    # Same run, now with an identified caller (a login that failed to resolve
    # an org on the first write is exactly how this arises).
    r.record_start("run-1", USER_A, "q", "running", robot_code="code-1")

    # org_id is write-once on the row, so the repair the fallback enables is
    # visible in what _attach_test hands the upsert, not in the stored value.
    with r._pool.connection() as conn:
        _, _, resolved = r._attach_test(
            conn, "run-1", "alice", "a@x.com", "q", "code-1", None, "org-a")
    assert resolved == "org-a"


def test_the_test_org_still_wins_when_it_has_one(reg):
    """The fallback must not become 'the caller wins' — that would undo D6."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated", robot_code="code-1")

    with r._pool.connection() as conn:
        _, _, resolved = r._attach_test(
            conn, "run-2", "root", "r@x.com", "q", "code-1", "run-1",
            "org-ADMIN")
    assert resolved == "org-a"


def test_the_backfill_carries_the_org_onto_the_test_not_only_the_run(reg):
    """The migration's own hole. backfill_data_org_ids reaches this through
    get_run_registry(), whose construction has ALREADY run the collapse — so
    the collapse mints tests.org_id NULL and the run backfill would otherwise
    leave tests and test_runs disagreeing, breaking D6 at the source."""
    r, admin = reg
    user_uuid = str(uuid.uuid4())
    org = str(uuid.uuid4())

    # An org-less run that nonetheless has an identified user: the state a
    # login-time org failure leaves behind. org_members does not exist yet, so
    # _lookup_org_id swallows and returns None — which is the point, and is
    # also why the membership is created only afterwards, exactly as the
    # backfill's real ordering has it.
    r.record_start("run-1", {"user_id": user_uuid, "email": "u@x.com"},
                   "q", "generated", robot_code="code-1")
    test_id, _, org_id, _ = _row(admin, "run-1")
    assert org_id is None
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] is None

    admin.execute(
        "CREATE TABLE org_members (org_id UUID, user_id UUID,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    admin.execute("INSERT INTO org_members (org_id, user_id) VALUES (%s, %s)",
                  (org, user_uuid))

    assert r.backfill_org_ids() == 1     # still counts RUNS, unchanged

    run_org = _row(admin, "run-1")[2]
    test_org = admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0]
    assert run_org == org
    assert test_org == run_org, "tests and test_runs disagree — D6 is broken"
    # Idempotent: a second pass moves nothing.
    assert r.backfill_org_ids() == 0
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] == org


def test_the_backfill_renumbers_a_test_moving_into_an_occupied_org(reg):
    """org_id is half of the (org_id, key_n) unique index. A test carrying
    key_n 1 out of the NULL bucket into an org that already has a key_n 1
    would raise UniqueViolation and abort the whole backfill — every other
    table's org repair lost with it, since backfill_data_org_ids only logs."""
    r, admin = reg
    user_uuid = str(uuid.uuid4())
    org = str(uuid.uuid4())

    # The org already owns key_n 1 and 2 (org supplied on the token).
    r.record_start("run-a", {"user_id": user_uuid, "email": "u@x.com",
                             "org_id": org}, "q1", "generated", robot_code="c")
    r.record_start("run-b", {"user_id": user_uuid, "email": "u@x.com",
                             "org_id": org}, "q2", "generated", robot_code="c")
    # Two org-less tests, both numbered from the NULL bucket. org_members does
    # not exist yet, so _lookup_org_id cannot rescue them at write time.
    r.record_start("run-1", {"user_id": user_uuid, "email": "u@x.com"},
                   "q3", "generated", robot_code="c")
    r.record_start("run-2", {"user_id": user_uuid, "email": "u@x.com"},
                   "q4", "generated", robot_code="c")
    assert sorted(k for (k,) in admin.execute(
        "SELECT key_n FROM tests WHERE org_id IS NULL").fetchall()) == [1, 2]

    admin.execute(
        "CREATE TABLE org_members (org_id UUID, user_id UUID,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    admin.execute("INSERT INTO org_members (org_id, user_id) VALUES (%s, %s)",
                  (org, user_uuid))

    assert r.backfill_org_ids() == 2

    keys = sorted(k for (k,) in admin.execute(
        "SELECT key_n FROM tests WHERE org_id = %s", (org,)).fetchall())
    assert keys == [1, 2, 3, 4], f"keys collided or were not reassigned: {keys}"
    assert admin.execute(
        "SELECT count(*) FROM tests WHERE org_id IS NULL").fetchone()[0] == 0
