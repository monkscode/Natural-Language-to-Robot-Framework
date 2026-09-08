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


def _a_two_version_test(r, admin):
    """Seed a test with two versions the only way P1 can produce one: by
    letting a RunRegistry construction collapse pre-split rows.

    _attach_test mints exactly one version per test and never a second, so no
    sequence of record_start calls reaches two versions. Hand-inserting the
    second test_versions row would work, but it would assert against a shape
    built by the test rather than by the code; the collapse builds the shape
    the live defect was found in — 7 of the owner's 21 migrated tests carry
    two or more versions, one of them ten.

    Three runs on one query: two with code (versions 1 and 2, chronological)
    and one without, which D8 attaches carrying test_version_id NULL. Returns
    (test_id, version 1, version 2)."""
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " robot_code, status, org_id, created_at) VALUES"
        " ('src-v1', 'alice', 'a@x.com', 'q', 'code-1', 'passed', 'org-a',"
        " '2026-01-01T10:00:00Z'),"
        " ('src-v2', 'alice', 'a@x.com', 'q', 'code-2', 'passed', 'org-a',"
        " '2026-01-02T10:00:00Z'),"
        " ('src-none', 'alice', 'a@x.com', 'q', NULL, 'error', 'org-a',"
        " '2026-01-03T10:00:00Z')")
    from src.backend.core.run_registry import RunRegistry
    # r.dsn pins search_path to the fixture's throwaway schema, so this
    # collapses the rows seeded above and nothing else.
    RunRegistry(dsn=r.dsn).close()

    test_id, v1, _, _ = _row(admin, "src-v1")
    v2 = _row(admin, "src-v2")[1]
    assert admin.execute(
        "SELECT current_version FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] == 2
    assert v1 is not None and v2 is not None and v1 != v2
    assert _row(admin, "src-none")[1] is None
    return test_id, v1, v2


def test_a_rerun_names_the_version_whose_code_it_actually_runs(reg):
    """A re-run re-executes the SOURCE RUN's stored code verbatim — the rerun
    endpoint passes `resolve_robot_code(source)` — so the version it names
    must be the one that code came from. Resolving the test's current_version
    instead stamps the new run with a version it never ran: reproduced through
    POST /execute-test on a live stack, where re-running a version-1 run
    produced a row pointing at version 2."""
    r, admin = reg
    test_id, v1, _v2 = _a_two_version_test(r, admin)

    r.record_start("rerun-1", USER_A, "q", "running",
                   robot_code="code-1", rerun_of="src-v1")

    run_test, version_id, _, _ = _row(admin, "rerun-1")
    assert run_test == test_id
    assert version_id == v1, "the re-run names a version it did not run"
    ran, named = admin.execute(
        "SELECT r.robot_code, v.robot_code FROM test_runs r"
        " JOIN test_versions v ON v.version_id = r.test_version_id"
        " WHERE r.run_id = 'rerun-1'").fetchone()
    assert named == ran


def test_a_rerun_of_the_newest_version_still_names_the_newest(reg):
    """The half the current-version lookup got right, kept: when the source
    run IS the newest, both readings agree and the answer must not move."""
    r, admin = reg
    _test_id, _v1, v2 = _a_two_version_test(r, admin)

    r.record_start("rerun-2", USER_A, "q", "running",
                   robot_code="code-2", rerun_of="src-v2")

    assert _row(admin, "rerun-2")[1] == v2


def test_a_rerun_of_a_code_less_run_falls_back_to_the_current_version(reg):
    """D8's code-less run attaches to its test with test_version_id NULL, and
    it is still re-runnable — resolve_robot_code falls back to the stored
    test.robot artifact. Reading the source's NULL straight through would
    leave the new run unversioned, so the current-version lookup has to stay
    as the fallback."""
    r, admin = reg
    test_id, _v1, v2 = _a_two_version_test(r, admin)

    r.record_start("rerun-3", USER_A, "q", "running",
                   robot_code="recovered-code", rerun_of="src-none")

    run_test, version_id, _, _ = _row(admin, "rerun-3")
    assert run_test == test_id
    assert version_id == v2, "a NULL source version must not become a NULL run"


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


def test_the_admin_flag_is_write_once_across_a_re_upsert(reg):
    """Task 2 corner case: a user promoted to admin between the opening
    'running' row and the terminal write. ran_as_platform_admin is write-once
    by OMISSION from the ON CONFLICT body (record_start's own docstring), so
    the row must keep the value from the write that CREATED it, not the
    caller's later authority."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "running")
    assert _row(admin, "run-1")[3] is False

    r.record_start("run-1", USER_A, "q", "generated", robot_code="c",
                   is_platform_admin=True)
    assert _row(admin, "run-1")[3] is False, (
        "ran_as_platform_admin must stay False — set on the write that "
        "created the row, not a later one")


def test_a_platform_admin_does_not_write_back_the_org_on_the_existing_branch(reg):
    """Owner ruling, 2026-09-08 (Task 2 item 6). The write-back guard is
    about authority, not identity: when the caller holds platform-admin
    authority, _attach_test must skip _write_back_org — the run's OWN org
    still lands on the row exactly as before, only the TEST is left alone.

    Reached through the 'existing' branch: the row's own test_id is already
    attached (a second upsert of the same run_id), and the joined test's org
    is NULL."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]
    assert _row(admin, "run-1")[2] is None

    admin_user = {"user_id": "root", "org_id": "org-ADMIN", "email": "r@x.com"}
    r.record_start("run-1", admin_user, "q", "running", robot_code="code-1",
                   is_platform_admin=True)

    # Accepted cost: the test stays org-less forever.
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == (None, 1)
    # ...but the RUN still takes the admin's own org. Nothing regresses.
    assert _row(admin, "run-1")[2] == "org-ADMIN"


def test_a_platform_admin_does_not_write_back_the_org_on_the_rerun_branch(reg):
    """Same guard, the rerun_of branch: a platform admin re-running another
    caller's org-less test must not claim it either."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    source_test = _row(admin, "run-1")[0]
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (source_test,)).fetchone()[0] is None

    admin_user = {"user_id": "root", "org_id": "org-ADMIN", "email": "r@x.com"}
    r.record_start("run-2", admin_user, "q", "running",
                   robot_code="code-1", rerun_of="run-1",
                   is_platform_admin=True)

    test_id, _, org_id, flag = _row(admin, "run-2")
    assert test_id == source_test
    assert org_id == "org-ADMIN"     # the RUN still takes the admin's org
    assert flag is True
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (source_test,)).fetchone() == (None, 1), (
        "a platform admin's write-back must be skipped — the test stays "
        "org-less")


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
    swallowing a failure writes one on the upsert that creates the test.
    This now repairs the TEST as well as the run: the source test's own
    org_id must not still be NULL once an identified caller reruns it."""
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
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (source_test,)).fetchone() == ("org-a", 1), (
        "the source test itself must be repaired, not only the new run")


def test_the_caller_org_fills_in_when_the_rows_own_test_has_none(reg):
    """Same rule, the idempotent branch — the second and third upsert of one
    run read the test's org back rather than the caller's, so the NULL has to
    be caught on that path too or the run is org-less from its second write
    onward. This now repairs the TEST itself on the second call, and the
    third call — where the test is no longer NULL — must be a no-op rather
    than a second write-back (no error, key_n untouched)."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]
    assert _row(admin, "run-1")[2] is None
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == (None, 1)

    # Same run, now with an identified caller (a login that failed to resolve
    # an org on the first write is exactly how this arises).
    r.record_start("run-1", USER_A, "q", "running", robot_code="code-1")

    assert _row(admin, "run-1")[2] == "org-a"
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == ("org-a", 1)

    # Third upsert of the same run (execute-start): the test's org is no
    # longer NULL, so this must be a no-op, not a second write-back attempt.
    r.record_start("run-1", USER_A, "q", "passed", robot_code="code-1")
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == ("org-a", 1)


def test_the_test_org_still_wins_when_it_has_one(reg):
    """The fallback must not become 'the caller wins' — that would undo D6.
    No write-back may be attempted at all here: the test already has an
    org, so it must be untouched (org_id AND key_n) after a cross-org
    platform-admin rerun tries to claim it."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]

    with r._pool.connection() as conn:
        _, _, resolved = r._attach_test(
            conn, "run-2", "root", "r@x.com", "q", "code-1", "run-1",
            "org-ADMIN")
    assert resolved == "org-a"
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == ("org-a", 1), "org-ADMIN must not win"


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


def test_a_failed_tests_backfill_does_not_discard_the_runs_backfill(reg):
    """The two UPDATEs share one transaction before the commit, so without a
    SAVEPOINT a failure in the second would roll the FIRST one back as well —
    and that loss is permanent: backfill_org_ids swallows and returns 0,
    migrate() returns normally, and run_migration_once then writes the
    data_org_id_backfill marker unconditionally (migration_state.py:89-95), so
    the backfill never runs again. The tests repair may fail; it may not take
    the runs repair with it."""
    from contextlib import contextmanager

    r, admin = reg
    user_uuid = str(uuid.uuid4())
    org = str(uuid.uuid4())

    r.record_start("run-1", {"user_id": user_uuid, "email": "u@x.com"},
                   "q", "generated", robot_code="code-1")
    test_id, _, org_id, _ = _row(admin, "run-1")
    assert org_id is None

    admin.execute(
        "CREATE TABLE org_members (org_id UUID, user_id UUID,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    admin.execute("INSERT INTO org_members (org_id, user_id) VALUES (%s, %s)",
                  (org, user_uuid))

    class _FailsTheTestsUpdate:
        def __init__(self, real):
            self._real = real
            self.fired = False

        def execute(self, sql, params=None):
            if sql.lstrip().startswith("UPDATE tests"):
                self.fired = True
                # A REAL server-side UniqueViolation, on the very index the
                # live failure mode names. Raising from Python instead would
                # leave the transaction healthy, and the assertions below
                # would then pass with no savepoint in the code at all.
                return self._real.execute(
                    "INSERT INTO tests (test_id, org_id, key_n,"
                    " current_version) SELECT %s, org_id, key_n, 1"
                    " FROM tests LIMIT 1", (str(uuid.uuid4()),))
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connection = r._pool.connection
    seen = {}

    @contextmanager
    def _wrapped(*args, **kwargs):
        with real_connection(*args, **kwargs) as conn:
            yield seen.setdefault("conn", _FailsTheTestsUpdate(conn))

    with patch.object(r._pool, "connection", _wrapped):
        updated = r.backfill_org_ids()

    assert seen["conn"].fired, "the tests UPDATE never ran"
    # The runs count, not 0 — the marker is written either way, so a 0 here
    # would report "nothing to do" for a boot that in fact repaired a row.
    assert updated == 1
    # Read back on a SEPARATE connection: this proves the runs repair was
    # COMMITTED, not merely pending in the rolled-back transaction.
    assert _row(admin, "run-1")[2] == org
    # ...and only the tests repair was lost, which the next boot can redo.
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] is None


def test_no_write_back_when_neither_caller_nor_the_test_has_an_org(reg):
    """Task 1 corner case: a token-less caller reaches a test that is
    already org-less (the bench, or AUTH_ENFORCED=false). The write-back
    guard is `org_id is not None`, and here the caller's org_id is also
    None, so nothing is written and nothing raises."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]

    r.record_start("run-1", None, "q", "running", robot_code="code-1")

    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == (None, 1)
    assert _row(admin, "run-1")[2] is None


def test_no_write_back_when_the_caller_has_none_but_the_test_already_does(reg):
    """Task 1 corner case: an identified caller mints the test; a later
    token-less caller of the SAME run (e.g. a retry that lost its token)
    must not blank the test's org or touch key_n — the test's own org
    already wins, unchanged. Same rule as
    test_the_test_org_still_wins_when_it_has_one, but reached through the
    short-circuit branch instead of rerun_of."""
    r, admin = reg
    r.record_start("run-1", USER_A, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]

    r.record_start("run-1", None, "q", "running", robot_code="code-1")

    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == ("org-a", 1)
    assert _row(admin, "run-1")[2] == "org-a"


def test_the_write_back_renumbers_key_n_into_an_occupied_org_bucket(reg):
    """Task 1, item 2: org_id is half of idx_tests_org_key (UNIQUE
    (org_id, key_n) NULLS NOT DISTINCT), so moving a test out of the NULL
    bucket into an org that already holds key_n 1 and 2 must land on 3, not
    collide, and not raise."""
    r, admin = reg
    r.record_start("run-a", USER_A, "q1", "generated", robot_code="c")
    r.record_start("run-b", USER_A, "q2", "generated", robot_code="c")
    assert sorted(k for (k,) in admin.execute(
        "SELECT key_n FROM tests WHERE org_id = 'org-a'"
    ).fetchall()) == [1, 2]

    r.record_start("run-1", None, "q3", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]
    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == (None, 1)

    r.record_start("run-1", USER_A, "q3", "running", robot_code="code-1")

    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == ("org-a", 3)
    assert sorted(k for (k,) in admin.execute(
        "SELECT key_n FROM tests WHERE org_id = 'org-a'"
    ).fetchall()) == [1, 2, 3], "the move must not collide with the occupied bucket"
    assert _row(admin, "run-1")[2] == "org-a"


class _CollidesOnceOnOrgWriteBack:
    """A connection wrapper whose FIRST org write-back UPDATE raises
    UniqueViolation, exactly as Postgres does when two write-backs (or a
    write-back and a fresh test INSERT) target the same destination org's
    next key_n at once. Task 1 item 4 says the write-back does not retry
    on this — unlike the key-collision retry on test creation above.

    This is a REPRESENTATIVE failure, not the only one the write-back's
    `except Exception` now catches (review round 1, Important 1): the
    write-back turned two previously read-only branches into branches that
    write, which is also deadlock/lock-timeout/serialization-failure/
    dropped-connection territory. See
    test_a_real_postgres_collision_still_leaves_the_run_recorded below for
    a GENUINE Postgres-raised UniqueViolation, which this Python injection
    cannot substitute for when what is under test is whether the SAVEPOINT
    actually leaves the connection usable afterward."""

    def __init__(self, real):
        self._real = real
        self.collisions = 0

    def execute(self, sql, params=None):
        if "UPDATE tests SET org_id" in sql and self.collisions == 0:
            self.collisions += 1
            raise psycopg.errors.UniqueViolation(
                'duplicate key value violates unique constraint'
                ' "idx_tests_org_key"')
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_write_back_collision_leaves_the_run_recorded_and_the_test_org_less(
        reg, caplog):
    """Task 1 item 4: a concurrent claim on the destination org's next
    key_n must not take the run write down with it, and must not spin
    retrying either — the run still gets the caller's org, the test stays
    NULL, and the next write to reach it repeats the repair."""
    import logging

    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]

    real_attach = type(r)._attach_test
    seen = {}

    def _through_a_colliding_connection(self, conn, *args, **kwargs):
        wrapper = seen.setdefault("conn", _CollidesOnceOnOrgWriteBack(conn))
        return real_attach(self, wrapper, *args, **kwargs)

    with caplog.at_level(logging.WARNING, logger="src.backend.core.run_registry"), \
            patch.object(type(r), "_attach_test", _through_a_colliding_connection):
        r.record_start("run-1", USER_A, "q", "running", robot_code="code-1")

    assert seen["conn"].collisions == 1
    assert _row(admin, "run-1")[2] == "org-a", (
        "the run must still get the caller's org")
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] is None, "the collision must not retry"
    assert any("org write-back failed" in m for m in caplog.messages)


def test_a_real_postgres_collision_still_leaves_the_run_recorded(reg):
    """Review round 1, Important 3 ('cannot verify'): the Python injection
    above proves the except branch runs; it proves nothing about psycopg's
    transaction state, which is the entire reason the write-back uses a
    SAVEPOINT (`with conn.transaction():`) in the first place. This drives
    a GENUINE aborted subtransaction instead.

    A second, independent connection to the same schema opens its own
    transaction and INSERTs the exact (org_id, key_n) row the write-back is
    about to compute for an empty 'org-a' bucket — (`'org-a'`, 1) — but does
    NOT commit yet. record_start's write-back then computes the SAME key_n
    (it cannot see the racer's uncommitted row) and tries to write it too;
    Postgres blocks that statement on the racer's held row lock rather than
    raising immediately. Only once the racer commits does the blocked
    statement resume and get a REAL UniqueViolation from the server.

    If the SAVEPOINT were missing or misplaced, the aborted subtransaction
    would poison the rest of THIS connection's transaction, and the run
    UPSERT that record_start issues immediately afterward — on that same
    connection — would fail with psycopg's 'current transaction is
    aborted' instead of succeeding. That the run row exists afterward is
    the assertion that actually exercises the SAVEPOINT, not merely the
    except clause around it."""
    import threading
    import time

    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]

    racer = psycopg.connect(r.dsn, autocommit=False)
    racer_inserted = threading.Event()
    release_racer = threading.Event()

    def _hold_the_key():
        try:
            racer.execute(
                "INSERT INTO tests (test_id, org_id, key_n, current_version)"
                " VALUES (%s, 'org-a', 1, 1)", (str(uuid.uuid4()),))
            racer_inserted.set()
            release_racer.wait(timeout=5)
            racer.commit()
        finally:
            racer.close()

    racer_thread = threading.Thread(target=_hold_the_key)
    racer_thread.start()
    assert racer_inserted.wait(timeout=5), "racer never inserted its row"

    def _release_shortly_after():
        # Give record_start's UPDATE time to reach Postgres and block on
        # the racer's row lock before the racer resolves it.
        time.sleep(0.3)
        release_racer.set()

    releaser = threading.Thread(target=_release_shortly_after)
    releaser.start()

    r.record_start("run-1", USER_A, "q", "running", robot_code="code-1")

    racer_thread.join(timeout=5)
    releaser.join(timeout=5)

    # The run must still be written, on the SAME connection the aborted
    # write-back ran on -- this is what a missing/misplaced SAVEPOINT would
    # break (either no row at all, or the pool raising on the next use).
    assert admin.execute(
        "SELECT count(*) FROM test_runs WHERE run_id = 'run-1'"
    ).fetchone()[0] == 1
    assert _row(admin, "run-1")[2] == "org-a", (
        "the run must still get the caller's org")
    # The racer's row won the (org_id, key_n) slot; the test stays NULL.
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] is None


def test_the_write_back_is_redone_after_the_folder_vanished_retry(reg):
    """Task 1's seventh corner case. record_start's folder-vanished retry
    calls _attach_test a SECOND time after a bare conn.rollback() — a full
    rollback, not a savepoint, so it discards everything the first attempt
    did on this connection, including a write-back UPDATE (the same reason
    it discards a freshly minted test/version row; record_start's own
    comment on the retry explains why). The second attempt must redo the
    repair rather than leave the test org-less because the first one was
    rolled back with everything else."""
    r, admin = reg
    r.record_start("run-1", None, "q", "generated", robot_code="code-1")
    test_id = _row(admin, "run-1")[0]
    assert admin.execute(
        "SELECT org_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] is None

    gid = r.create_group("org-a", "alice", "Doomed")["group_id"]
    real_guard = type(r)._fileable_group_id

    def _delete_after_reading(self, group_id, org_id):
        inherited = real_guard(self, group_id, org_id)
        admin.execute("DELETE FROM run_groups WHERE group_id = %s",
                      (group_id,))
        return inherited

    with patch.object(type(r), "_fileable_group_id", _delete_after_reading):
        r.record_start("run-1", USER_A, "q", "running",
                       robot_code="code-1", group_id=gid)

    assert admin.execute(
        "SELECT org_id, key_n FROM tests WHERE test_id = %s",
        (test_id,)).fetchone() == ("org-a", 1)
    assert _row(admin, "run-1")[2] == "org-a"


def test_d6_holds_after_a_representative_mix_of_mint_repair_and_rerun(reg):
    """Spec section 13 item 6's live check
    (SELECT count(*) FROM test_runs r JOIN tests te USING (test_id)
     WHERE r.org_id IS DISTINCT FROM te.org_id, IS DISTINCT FROM because
    both sides can be NULL), run against a converged mix: an org-less
    mint, an org-bearing mint, a write-back repair, and a rerun taken AFTER
    the repair. The one case that is NOT reproduced here is the deliberate
    write-back collision above, which is a known, accepted, self-healing
    EXCEPTION to this invariant (item 4) -- not a scenario this check
    claims to cover."""
    r, admin = reg
    r.record_start("run-1", None, "q1", "generated", robot_code="code-1")
    r.record_start("run-2", USER_A, "q2", "generated", robot_code="code-1")
    r.record_start("run-1", USER_A, "q1", "running", robot_code="code-1")
    r.record_start("run-3", USER_A, "q1", "running",
                   robot_code="code-1", rerun_of="run-1")

    violations = admin.execute(
        "SELECT count(*) FROM test_runs r JOIN tests te USING (test_id) "
        "WHERE r.org_id IS DISTINCT FROM te.org_id").fetchone()[0]
    assert violations == 0
