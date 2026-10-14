"""record_start appending a version to a test the CALLER names (Task 7).

The three existing _attach_test branches all DERIVE the test: from the run's
own row, from a rerun_of source, or by minting one. Task 7 adds the fourth
shape -- the caller hands record_start the test_id up front, because the
Update dialog is regenerating THAT test -- and it is the first write path
that can append version n+1 to a test somebody else authored.

What this file pins, each being a place the shape can go quietly wrong:

- the append advances all three facts together (a new test_versions row,
  tests.current_version and tests.user_query), because a version nobody
  points at is a version nobody runs;
- it is idempotent on the RUN: record_start is an upsert called at
  generation start, at generation success and again at execute, so a second
  success write must append nothing;
- it NEVER mints. A named test that does not exist is a caller error, and
  minting one would answer the wrong request silently;
- it never writes an org back onto the test -- naming a test is not a claim
  on it (owner ruling R7-8);
- concurrency is a ROW LOCK, not a retry loop. Three simultaneous appenders
  with a bounded UNIQUE retry were measured to lose one of them; the lock
  keeps all three (session 7 probe P1). The lock test below is the one that
  bites when it is removed.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (record_start, _attach_test,
get_test_head, get_run_version, get_test_detail).
"""
import threading
import time
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

USER_A = {"user_id": "alice", "org_id": "org-a", "email": "a@x.com"}
USER_A_NO_ORG = {"user_id": "alice", "email": "a@x.com"}
USER_B = {"user_id": "bob", "org_id": "org-b", "email": "b@x.com"}


@pytest.fixture
def reg():
    """A RunRegistry on a throwaway schema, plus an admin connection and the
    registry's own DSN (the lock test opens a second connection on it)."""
    name = f"tests_versions_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    admin.execute(f"SET search_path TO {name}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    dsn = settings.DATABASE_URL + f"{sep}options=-c%20search_path%3D{name}"
    from src.backend.core.run_registry import RunRegistry
    r = RunRegistry(dsn=dsn)
    try:
        yield r, admin, dsn
    finally:
        r.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _seed(r, admin, user=USER_A, query="search shoes", code="*** Tasks *** v1"):
    """One test at version 1, through the real mint path. Returns its id."""
    run_id = str(uuid.uuid4())
    r.record_start(run_id, user, query, "generated", robot_code=code)
    return admin.execute(
        "SELECT test_id FROM test_runs WHERE run_id = %s",
        (run_id,)).fetchone()[0]


def _run_row(admin, run_id):
    return admin.execute(
        "SELECT test_id, test_version_id, org_id FROM test_runs"
        " WHERE run_id = %s", (run_id,)).fetchone()


def _versions(admin, test_id):
    return admin.execute(
        "SELECT n, user_query, robot_code, created_by, created_by_email,"
        " reason FROM test_versions WHERE test_id = %s ORDER BY n",
        (test_id,)).fetchall()


def _test_row(admin, test_id):
    return admin.execute(
        "SELECT current_version, user_query, org_id, user_id FROM tests"
        " WHERE test_id = %s", (test_id,)).fetchone()


# ---------------------------------------------------------------------------
# The append itself
# ---------------------------------------------------------------------------

def test_appending_advances_the_version_the_pointer_and_the_description(reg):
    """All three, or the new version is written and never executed: every
    later run of this test resolves its code through tests.current_version."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="edited")

    assert [(v[0], v[2]) for v in _versions(admin, test_id)] == [
        (1, "*** Tasks *** v1"), (2, "*** Tasks *** v2")]
    current_version, user_query, _, _ = _test_row(admin, test_id)
    assert current_version == 2
    assert user_query == "search boots"
    attached_test, attached_version, _ = _run_row(admin, run_id)
    assert attached_test == test_id
    assert attached_version == admin.execute(
        "SELECT version_id FROM test_versions WHERE test_id = %s AND n = 2",
        (test_id,)).fetchone()[0]


def test_the_append_records_its_reason_its_author_and_that_authors_email(reg):
    """created_by_email is the appender's own, not the test author's: after
    Task 7 those two differ (owner ruling R7-4), and the drawer names a
    version's author from the email rather than the internal user id."""
    r, admin, _ = reg
    test_id = _seed(r, admin)

    r.record_start(str(uuid.uuid4()), USER_B, "search shoes", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="regenerated")

    n, version_query, _, created_by, created_email, reason = \
        _versions(admin, test_id)[1]
    assert (n, reason) == (2, "regenerated")
    assert (created_by, created_email) == ("bob", "b@x.com")
    assert version_query == "search shoes"
    # The test's own author is untouched -- appending is not authorship.
    assert _test_row(admin, test_id)[3] == "alice"


def test_the_mint_records_the_authors_email_on_version_one(reg):
    """The same column, written by the OTHER writer, so a version 1 and a
    version 2 answer "who wrote this" the same way."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    assert _versions(admin, test_id)[0][4] == "a@x.com"


def test_a_second_success_write_appends_nothing(reg):
    """record_start is an upsert called several times per run. Two versions
    from one regeneration is the failure this idempotency prevents."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    run_id = str(uuid.uuid4())
    kwargs = dict(robot_code="*** Tasks *** v2", test_id=test_id,
                  version_reason="edited")

    r.record_start(run_id, USER_A, "search boots", "generated", **kwargs)
    r.record_start(run_id, USER_A, "search boots", "generated", **kwargs)

    assert [v[0] for v in _versions(admin, test_id)] == [1, 2]
    assert _test_row(admin, test_id)[0] == 2


def test_the_opening_row_of_a_regeneration_attaches_with_no_version(reg):
    """Generation start carries no code and no reason: the run must show up
    ON the test (so "this test is regenerating" is visible) while writing no
    version at all."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, "search boots", "running", test_id=test_id)

    assert _run_row(admin, run_id)[:2] == (test_id, None)
    assert [v[0] for v in _versions(admin, test_id)] == [1]
    assert _test_row(admin, test_id)[0] == 1


def test_a_failed_regeneration_writes_no_version_and_moves_no_pointer(reg):
    """Spec case 10 / D8 (b): the failed run attaches to its test so the
    failure is visible on it, and the test is otherwise untouched."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, "search boots", "error",
                   error_message="planner failed", test_id=test_id)

    assert _run_row(admin, run_id)[:2] == (test_id, None)
    assert [v[0] for v in _versions(admin, test_id)] == [1]
    current_version, user_query, _, _ = _test_row(admin, test_id)
    assert (current_version, user_query) == (1, "search shoes")


def test_a_missing_target_test_mints_nothing(reg):
    """Naming a test that does not exist must not answer a different
    request. The run is recorded unattached -- a permanently legal state
    (D8) -- rather than a brand-new test the caller never asked for."""
    r, admin, _ = reg
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=str(uuid.uuid4()), version_reason="edited")

    assert admin.execute("SELECT count(*) FROM tests").fetchone()[0] == 0
    assert admin.execute(
        "SELECT count(*) FROM test_versions").fetchone()[0] == 0
    assert _run_row(admin, run_id)[:2] == (None, None)


def test_a_missing_target_test_still_leaves_a_history_row(reg):
    """The other half of the check above, and the half a foreign key cannot
    cover for it. Attaching a run to a test that does not exist violates
    fk_test_runs_test on the run INSERT itself, and record_start's folder
    retry cannot rescue that -- so the whole write is swallowed and the run
    is invisible everywhere. Refusing the attach costs the run its test;
    not refusing it costs the run its ROW."""
    r, admin, _ = reg
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, "search boots", "running",
                   test_id=str(uuid.uuid4()))

    row = _run_row(admin, run_id)
    assert row is not None, "the run was not recorded at all"
    assert row[:2] == (None, None)


def test_a_run_already_attached_elsewhere_is_never_re_homed(reg):
    """The run's own row wins. Re-homing would move a result off the test
    whose code it actually ran."""
    r, admin, _ = reg
    other = _seed(r, admin)
    target = _seed(r, admin, query="another test")
    run_id = str(uuid.uuid4())
    r.record_start(run_id, USER_A, "another test", "generated",
                   robot_code="*** Tasks *** other")
    # That mint made its own test; point the run at `other` by hand so the
    # run names a test the next call is NOT allowed to change.
    admin.execute("UPDATE test_runs SET test_id = %s, test_version_id = NULL"
                  " WHERE run_id = %s", (other, run_id))

    r.record_start(run_id, USER_A, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=target, version_reason="edited")

    assert _run_row(admin, run_id)[0] == other
    assert [v[0] for v in _versions(admin, target)] == [1]


# ---------------------------------------------------------------------------
# Executing a named version
# ---------------------------------------------------------------------------

def test_an_execute_attach_keeps_a_version_of_the_named_test(reg):
    r, admin, _ = reg
    test_id = _seed(r, admin)
    version_id = admin.execute(
        "SELECT version_id FROM test_versions WHERE test_id = %s",
        (test_id,)).fetchone()[0]
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, None, "running",
                   robot_code="*** Tasks *** v1",
                   test_id=test_id, test_version_id=version_id)

    assert _run_row(admin, run_id)[:2] == (test_id, version_id)
    assert [v[0] for v in _versions(admin, test_id)] == [1]


def test_an_execute_attach_refuses_a_version_of_another_test(reg):
    """The version id is client-reachable through the run it labels, so the
    pair is checked rather than trusted: a result must never claim to have
    run code belonging to a different test."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    foreign = _seed(r, admin, query="another test")
    foreign_version = admin.execute(
        "SELECT version_id FROM test_versions WHERE test_id = %s",
        (foreign,)).fetchone()[0]
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, None, "running",
                   robot_code="*** Tasks *** v1",
                   test_id=test_id, test_version_id=foreign_version)

    assert _run_row(admin, run_id)[:2] == (test_id, None)


# ---------------------------------------------------------------------------
# Org: the test wins, and naming it claims nothing
# ---------------------------------------------------------------------------

def test_the_run_takes_the_tests_org_not_the_callers(reg):
    """Owner decision D6, unchanged on the new branch."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_B, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="edited")

    assert _run_row(admin, run_id)[2] == "org-a"


def test_naming_a_test_never_writes_an_org_back_onto_it(reg):
    """Owner ruling R7-8. The two older branches repair an org-less test for
    its own author; this one cannot, because reaching it takes nothing but a
    test_id in a request body. The RUN still takes the caller's org, which is
    spec case 35's fallback."""
    r, admin, _ = reg
    test_id = _seed(r, admin, user=USER_A_NO_ORG)
    assert _test_row(admin, test_id)[2] is None
    run_id = str(uuid.uuid4())

    r.record_start(run_id, USER_A, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="edited")

    assert _test_row(admin, test_id)[2] is None
    assert _run_row(admin, run_id)[2] == "org-a"


# ---------------------------------------------------------------------------
# Concurrency: the row lock, not a retry loop
# ---------------------------------------------------------------------------

def test_the_append_serialises_behind_a_concurrent_writer(reg):
    """Deterministic stand-in for two simultaneous regenerations.

    A second connection takes the same FOR NO KEY UPDATE lock on the test
    row, inserts version 2, and holds its transaction open. With the lock in
    place the append blocks, and once the holder commits it recomputes
    max(n) + 1 and writes version 3. WITHOUT it the append reads max(n) = 1
    while the holder's version 2 is still invisible, blocks on the UNIQUE
    index instead, and loses to a UniqueViolation that record_start swallows
    -- leaving the run attached to no version at all.
    """
    r, admin, dsn = reg
    test_id = _seed(r, admin)
    holder = psycopg.connect(dsn)
    run_id = str(uuid.uuid4())
    finished = threading.Event()

    def _append():
        try:
            r.record_start(run_id, USER_A, "search boots", "generated",
                           robot_code="*** Tasks *** v3",
                           test_id=test_id, version_reason="edited")
        finally:
            finished.set()

    try:
        holder.execute("SELECT org_id FROM tests WHERE test_id = %s"
                       " FOR NO KEY UPDATE", (test_id,))
        holder.execute(
            "INSERT INTO test_versions (version_id, test_id, n, robot_code)"
            " VALUES (%s, %s, 2, %s)",
            (str(uuid.uuid4()), test_id, "*** Tasks *** held"))
        worker = threading.Thread(target=_append)
        worker.start()
        time.sleep(0.5)
        assert not finished.is_set(), "the append did not wait for the holder"
        holder.commit()
        worker.join(timeout=30)
        assert finished.is_set(), "the append never returned"
    finally:
        holder.close()

    assert [v[0] for v in _versions(admin, test_id)] == [1, 2, 3]
    assert _test_row(admin, test_id)[0] == 3
    assert _run_row(admin, run_id)[1] == admin.execute(
        "SELECT version_id FROM test_versions WHERE test_id = %s AND n = 3",
        (test_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# get_test_head -- the route's pre-flight read
# ---------------------------------------------------------------------------

def test_get_test_head_returns_the_head_and_the_current_versions_code(reg):
    r, admin, _ = reg
    test_id = _seed(r, admin)
    r.record_start(str(uuid.uuid4()), USER_A, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="edited")

    head = r.get_test_head(test_id, user_id="alice", org_id="org-a",
                           folder_org_id="org-a")
    assert head["test_id"] == test_id
    assert head["user_id"] == "alice"
    assert head["org_id"] == "org-a"
    assert head["user_query"] == "search boots"
    assert head["current_version"] == 2
    assert head["robot_code"] == "*** Tasks *** v2"
    assert head["version_id"] == admin.execute(
        "SELECT version_id FROM test_versions WHERE test_id = %s AND n = 2",
        (test_id,)).fetchone()[0]


def test_get_test_head_binds_the_same_visibility_the_drawer_binds(reg):
    """A test the Tests page will not list is not writable through a
    regeneration either -- one predicate, not two."""
    r, admin, _ = reg
    test_id = _seed(r, admin)

    assert r.get_test_head(test_id, user_id="bob", org_id="org-b",
                           folder_org_id="org-b") is None
    assert r.get_test_head(str(uuid.uuid4()), user_id="alice",
                           org_id="org-a", folder_org_id="org-a") is None


def test_get_test_head_reports_a_missing_current_version_row(reg):
    """Case 31's other half: the route answers 409 off these two keys, so
    the read has to distinguish "no such version row" from "no code"."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    admin.execute("UPDATE tests SET current_version = 7 WHERE test_id = %s",
                  (test_id,))

    head = r.get_test_head(test_id, user_id="alice", org_id="org-a",
                           folder_org_id="org-a")
    assert head["version_id"] is None
    assert head["robot_code"] is None


# ---------------------------------------------------------------------------
# get_run_version -- the stream's read-back
# ---------------------------------------------------------------------------

def test_get_run_version_names_the_test_and_the_version_number(reg):
    r, admin, _ = reg
    test_id = _seed(r, admin)
    run_id = str(uuid.uuid4())
    r.record_start(run_id, USER_A, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="edited")

    assert r.get_run_version(run_id) == (test_id, 2)


def test_get_run_version_is_none_for_an_unattached_or_unknown_run(reg):
    r, admin, _ = reg
    test_id = _seed(r, admin)
    opening = str(uuid.uuid4())
    r.record_start(opening, USER_A, "search boots", "running",
                   test_id=test_id)

    assert r.get_run_version(opening) == (test_id, None)
    assert r.get_run_version(str(uuid.uuid4())) == (None, None)


# ---------------------------------------------------------------------------
# get_test_detail names a version's author by email
# ---------------------------------------------------------------------------

def _detail_versions(r, test_id):
    detail = r.get_test_detail(test_id, user_id="alice", org_id="org-a",
                               folder_org_id="org-a")
    return {v["n"]: v for v in detail["versions"]}


def test_the_detail_returns_the_email_the_append_stored(reg):
    r, admin, _ = reg
    test_id = _seed(r, admin)
    r.record_start(str(uuid.uuid4()), USER_B, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="edited")

    assert _detail_versions(r, test_id)[2]["created_by_email"] == "b@x.com"


def test_a_legacy_version_falls_back_to_the_tests_own_email(reg):
    """Every row written before Task 7 has created_by = the test's author, by
    construction (both writers set it from the same value), so the test's own
    user_email is that version's author's email."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    admin.execute("UPDATE test_versions SET created_by_email = NULL"
                  " WHERE test_id = %s", (test_id,))

    assert _detail_versions(r, test_id)[1]["created_by_email"] == "a@x.com"


def test_the_fallback_does_not_fire_for_a_version_someone_else_wrote(reg):
    """Naming the test's author beside a version they did not write is the
    one thing this fallback must never do."""
    r, admin, _ = reg
    test_id = _seed(r, admin)
    r.record_start(str(uuid.uuid4()), USER_B, "search boots", "generated",
                   robot_code="*** Tasks *** v2",
                   test_id=test_id, version_reason="edited")
    admin.execute("UPDATE test_versions SET created_by_email = NULL"
                  " WHERE test_id = %s AND n = 2", (test_id,))

    assert _detail_versions(r, test_id)[2]["created_by_email"] is None


def test_an_author_less_version_gets_no_email(reg):
    """created_by NULL must not compare equal to a NULL tests.user_id."""
    r, admin, _ = reg
    test_id = _seed(r, admin, user=USER_A_NO_ORG)
    admin.execute("UPDATE tests SET user_id = NULL WHERE test_id = %s",
                  (test_id,))
    admin.execute("UPDATE test_versions SET created_by = NULL,"
                  " created_by_email = NULL WHERE test_id = %s", (test_id,))

    detail = r.get_test_detail(test_id, user_id=None, org_id=None)
    assert detail["versions"][0]["created_by_email"] is None
