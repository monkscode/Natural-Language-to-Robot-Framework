"""RunRegistry.assign_tests — filing a TEST into a folder (ruling R3).

The moving unit is the test, not one of its results: spec section 10 case 6
says a folder move "moves the TEST. Results follow by join". Three things
this file exists to pin, because each is a place the shape can go quietly
wrong:

- authority is read off the TEST's own columns (tests.org_id, tests.user_id).
  Deciding it from a RUN instead is this branch's signature defect, and here
  it would let a member file a test they do not own through a result they do;
- BOTH folder columns are written. Every caller-scoped read over runs
  resolves its folder as COALESCE(te.group_id, t.group_id), so clearing only
  tests.group_id leaves each run falling back to its own stale column and the
  org still reading every result of a test the caller has just made private;
- all-or-nothing. One test the caller may not file writes nothing at all,
  including the runs of the tests they COULD have filed.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (assign_tests, _visible_group,
_group_join, _VISIBLE_RUN_SQL, _VISIBLE_TEST_SQL).
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

ORG_A = "org-a"
ORG_B = "org-b"


@pytest.fixture
def reg():
    name = f"tests_assign_{uuid.uuid4().hex[:8]}"
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


# ---------------------------------------------------------------------------
# Seeding helpers -- direct SQL, for the same reason
# test_run_registry_get_test_detail.py uses it: assign_tests is a WRITER over
# shapes record_start cannot produce in one call (a peer's result of someone
# else's test, a run already filed into a different folder, an author-less
# test).
# ---------------------------------------------------------------------------

def _folder(admin, org=ORG_A, name=None):
    group_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO run_groups (group_id, name, org_id, created_by)"
        " VALUES (%s, %s, %s, 'alice')",
        (group_id, name or f"F-{group_id[:6]}", org))
    return group_id


def _test_row(admin, *, key_n, user_id="alice", org_id=ORG_A, group_id=None):
    test_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO tests (test_id, org_id, key_n, user_id, user_email,"
        " user_query, current_version, group_id)"
        " VALUES (%s, %s, %s, %s, 'a@x.com', 'q', 1, %s)",
        (test_id, org_id, key_n, user_id, group_id))
    version_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_versions (version_id, test_id, n, user_query,"
        " robot_code, created_by) VALUES (%s, %s, 1, 'q', '*** Tasks ***', %s)",
        (version_id, test_id, user_id))
    return test_id, version_id


def _run_row(admin, test_id, version_id, *, user_id="alice", org_id=ORG_A,
             group_id=None, status="passed"):
    run_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, test_id, test_version_id, group_id)"
        " VALUES (%s, %s, 'a@x.com', 'q', %s, %s, %s, %s, %s)",
        (run_id, user_id, status, org_id, test_id, version_id, group_id))
    return run_id


def _test_group(admin, test_id):
    """The raw admin connection has no dict row factory -- unlike the pool
    the registry itself uses -- so these two read by position."""
    return admin.execute(
        "SELECT group_id FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0]


def _run_group(admin, run_id):
    return admin.execute(
        "SELECT group_id FROM test_runs WHERE run_id = %s",
        (run_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# The happy path, and the second column that makes it honest.
# ---------------------------------------------------------------------------

def test_the_author_files_their_own_test_and_every_run_of_it_follows(reg):
    r, admin = reg
    group_id = _folder(admin)
    test_id, version_id = _test_row(admin, key_n=1)
    run_a = _run_row(admin, test_id, version_id)
    run_b = _run_row(admin, test_id, version_id, user_id="bob")
    before = admin.execute(
        "SELECT updated_at FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0]

    assert r.assign_tests(ORG_A, "alice", False, [test_id], group_id) is True

    assert _test_group(admin, test_id) == group_id
    # A move is a change to the test, and the drawer reads updated_at.
    assert admin.execute(
        "SELECT updated_at FROM tests WHERE test_id = %s",
        (test_id,)).fetchone()[0] > before
    # Both runs of the test carry it too -- spec section 4.4 keeps
    # test_runs.group_id populated and readable until P3 removes it.
    assert _run_group(admin, run_a) == group_id
    assert _run_group(admin, run_b) == group_id


def test_ungrouping_clears_the_runs_own_column_not_just_the_tests(reg):
    """The measured reason assign_tests writes both columns.

    Every caller-scoped read over runs resolves its folder as
    COALESCE(te.group_id, t.group_id), so a test-only ungroup leaves each run
    falling back to its own stale column: _VISIBLE_RUN_SQL's published term
    stays TRUE and a peer keeps reading the result of a test that has just
    disappeared from their Tests page. Measured on a throwaway schema before
    this was written: run 1 / test 0 after clearing tests.group_id alone."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, version_id = _test_row(admin, key_n=1, group_id=group_id)
    run_id = _run_row(admin, test_id, version_id, group_id=group_id)

    # A peer in the same org reads the filed result and the filed test.
    assert r.list_runs(user_id="bob", org_id=ORG_A, folder_org_id=ORG_A)[1] == 1
    assert r.list_tests(user_id="bob", org_id=ORG_A, folder_org_id=ORG_A)[1] == 1

    assert r.assign_tests(ORG_A, "alice", False, [test_id], None) is True

    assert _test_group(admin, test_id) is None
    assert _run_group(admin, run_id) is None
    assert r.list_runs(user_id="bob", org_id=ORG_A, folder_org_id=ORG_A)[1] == 0
    assert r.list_tests(user_id="bob", org_id=ORG_A, folder_org_id=ORG_A)[1] == 0


def test_a_peers_result_of_my_test_travels_with_the_test(reg):
    """Deliberate, and the same rule assign_runs' cascade already applies in
    the other direction: a result is owned by whoever fired it, but the
    TEST's author governs the test's publication, so filing the test moves
    every result under it whoever owns them."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, version_id = _test_row(admin, key_n=1)
    peer_run = _run_row(admin, test_id, version_id, user_id="bob")

    assert r.assign_tests(ORG_A, "alice", False, [test_id], group_id) is True
    assert _run_group(admin, peer_run) == group_id


# ---------------------------------------------------------------------------
# Authority -- read off the TEST's own columns, never off a result's.
# ---------------------------------------------------------------------------

def test_an_org_admin_files_a_members_test(reg):
    r, admin = reg
    group_id = _folder(admin)
    test_id, _ = _test_row(admin, key_n=1, user_id="bob")

    assert r.assign_tests(ORG_A, "alice", True, [test_id], group_id) is True
    assert _test_group(admin, test_id) == group_id


def test_a_member_cannot_file_a_peers_test_and_nothing_moves(reg):
    """Seeing a shared test is not authority over it (owner decision D4) --
    and the run under it must not move either, which is the check that fails
    if the authority filter is read off test_runs instead of tests."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, version_id = _test_row(admin, key_n=1, user_id="bob")
    # The caller owns a RESULT of the peer's test. That must confer nothing.
    own_run = _run_row(admin, test_id, version_id, user_id="alice")

    assert r.assign_tests(ORG_A, "alice", False, [test_id], group_id) is False
    assert _test_group(admin, test_id) is None
    assert _run_group(admin, own_run) is None


def test_an_author_less_test_is_filable_by_an_org_admin_and_not_by_a_member(reg):
    """tests.user_id NULL: the org_admin branch tests the org, so it passes;
    the member branch compares NULL to a concrete id, which is NULL rather
    than TRUE, so it fails closed. Same shape as assign_runs for an unowned
    run in the org."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, _ = _test_row(admin, key_n=1, user_id=None)

    assert r.assign_tests(ORG_A, "alice", False, [test_id], group_id) is False
    assert _test_group(admin, test_id) is None
    assert r.assign_tests(ORG_A, "alice", True, [test_id], group_id) is True
    assert _test_group(admin, test_id) == group_id


# ---------------------------------------------------------------------------
# The org terms -- on the folder, and on the test itself.
# ---------------------------------------------------------------------------

def test_a_folder_in_another_org_is_refused(reg):
    r, admin = reg
    foreign = _folder(admin, org=ORG_B)
    test_id, _ = _test_row(admin, key_n=1)

    assert r.assign_tests(ORG_A, "alice", False, [test_id], foreign) is False
    assert _test_group(admin, test_id) is None


def test_a_test_in_another_org_is_refused_even_for_its_author(reg):
    """The org term is on the TEST, org_admin or not. Without it a member who
    has left org B could file a test they still author there into a folder of
    the org they are in now, leaving a row whose org and folder disagree."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, _ = _test_row(admin, key_n=1, org_id=ORG_B)

    assert r.assign_tests(ORG_A, "alice", False, [test_id], group_id) is False
    assert r.assign_tests(ORG_A, "alice", True, [test_id], group_id) is False
    assert _test_group(admin, test_id) is None


def test_an_org_less_test_is_refused(reg):
    """tests.org_id NULL matches no org: run_groups.org_id is NOT NULL, so
    there is no folder such a test could legitimately sit in."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, _ = _test_row(admin, key_n=1, org_id=None)

    assert r.assign_tests(ORG_A, "alice", False, [test_id], group_id) is False
    assert _test_group(admin, test_id) is None


def test_a_caller_with_no_org_is_refused(reg):
    """No org means no folder to file into and nothing to test the test
    against.

    What this test CANNOT distinguish, measured rather than assumed: the
    `org_id is None` early-out is redundant for a non-empty batch, so
    removing it leaves every assertion here green. A named folder still
    fails _visible_group (run_groups.org_id is NOT NULL, so nothing equals
    NULL) and an ungroup still fails the org term in the UPDATE. The guard
    is an early-out that takes no connection, not the refusal."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, _ = _test_row(admin, key_n=1)

    assert r.assign_tests(None, "alice", False, [test_id], group_id) is False
    assert r.assign_tests(None, "alice", False, [test_id], None) is False
    assert _test_group(admin, test_id) is None


# ---------------------------------------------------------------------------
# All-or-nothing, and the batch's edges.
# ---------------------------------------------------------------------------

def test_a_mixed_batch_writes_nothing(reg):
    r, admin = reg
    group_id = _folder(admin)
    mine, mine_v = _test_row(admin, key_n=1)
    mine_run = _run_row(admin, mine, mine_v)
    theirs, _ = _test_row(admin, key_n=2, user_id="bob")

    assert r.assign_tests(ORG_A, "alice", False, [mine, theirs], group_id) is False
    assert _test_group(admin, mine) is None
    assert _test_group(admin, theirs) is None
    # The rollback takes the run write with it, not just the test write.
    assert _run_group(admin, mine_run) is None


def test_duplicate_ids_in_one_batch_count_once(reg):
    """The rowcount check compares against the DISTINCT ids, so naming one
    test twice is one move rather than an atomicity failure."""
    r, admin = reg
    group_id = _folder(admin)
    test_id, _ = _test_row(admin, key_n=1)

    assert r.assign_tests(ORG_A, "alice", False, [test_id, test_id],
                          group_id) is True
    assert _test_group(admin, test_id) == group_id


def test_an_unknown_test_id_is_refused(reg):
    r, admin = reg
    group_id = _folder(admin)

    assert r.assign_tests(ORG_A, "alice", False, [str(uuid.uuid4())],
                          group_id) is False


def test_filing_one_test_leaves_another_tests_runs_alone(reg):
    """The run write is scoped through the named tests, so a sibling test in
    the same org and the same folder-less state does not move with it."""
    r, admin = reg
    group_id = _folder(admin)
    moved, moved_v = _test_row(admin, key_n=1)
    moved_run = _run_row(admin, moved, moved_v)
    other, other_v = _test_row(admin, key_n=2)
    other_run = _run_row(admin, other, other_v)

    assert r.assign_tests(ORG_A, "alice", False, [moved], group_id) is True
    assert _run_group(admin, moved_run) == group_id
    assert _test_group(admin, other) is None
    assert _run_group(admin, other_run) is None


def test_the_folder_vanishing_mid_write_is_refused(reg):
    """assign_tests loses the race: the folder clears the authority check and
    is gone by the time the UPDATE runs. It must fail closed -- False (which
    the endpoint turns into a 404), no exception, nothing written -- and
    leave the pooled connection usable."""
    from unittest.mock import patch
    r, admin = reg
    test_id, version_id = _test_row(admin, key_n=1)
    run_id = _run_row(admin, test_id, version_id)
    ghost = str(uuid.uuid4())

    with patch.object(r, "_visible_group",
                      return_value={"group_id": ghost, "created_by": "alice"}):
        assert r.assign_tests(ORG_A, "alice", False, [test_id], ghost) is False

    # A connection poisoned by the caught violation shows up here as a
    # legible assertion rather than as an error inside the next read.
    assert _test_group(admin, test_id) is None
    assert _run_group(admin, run_id) is None
    assert r.list_tests(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A)[1] == 1
