"""The move writers' lock order — assign_runs and delete_group vs assign_tests.

Every writer that touches both `tests` and `test_runs` has to take them in the
SAME order or two of them eventually wait on each other and Postgres kills one.
The order is not a free choice: `record_start` calls `_attach_test` before its
own run upsert, so every write path through it — the mint, the org write-back
and the version append — touches `tests` first by construction, and
`record_start` is also the one party that swallows its own abort, so a deadlock
there loses a history row silently. Tests-first is therefore forced, and
`assign_tests` already obeys it (its `tests` UPDATE precedes its `test_runs`
one).

Two writers did not. `assign_runs` is three statements — two over `test_runs`,
then one over `tests` — so it ran runs-first. `delete_group` is a single
`DELETE FROM run_groups` whose two cascades are foreign-key ACTIONS, so its
order was decided by trigger name and by no line of code at all; measured on a
fresh database it took the result row first. Each therefore deadlocked with
`assign_tests`, and no route handler catches that: the registry methods catch
only ForeignKeyViolation, so DeadlockDetected escapes to the caller.

Both tests here drive the counterparty with `assign_tests`' own two statements
on a separate connection, so the interleave can be paused between them; the
registry method under test runs unmodified on a thread. The assertion is that
NEITHER party aborts — a deadlock always kills exactly one, so this is
deterministic whichever one Postgres picks as the victim.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (assign_runs, delete_group,
_visible_group).
"""
import threading
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

ORG = "org-lock"
USER = "alice"

# Long enough for the thread to reach its block and for Postgres to have
# decided, short enough that a wedged test fails the suite instead of
# stalling it. deadlock_timeout defaults to 1s, so detection lands inside it.
_SETTLE = 2.0


@pytest.fixture
def reg():
    """A registry on a throwaway schema, with a lock_timeout on every
    connection it hands out. Without that bound a regression here would hang
    the whole suite rather than fail this file."""
    name = f"lockorder_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(settings.DATABASE_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {name}")
    admin.execute(f"SET search_path TO {name}")
    sep = "&" if "?" in settings.DATABASE_URL else "?"
    opts = f"-c%20search_path%3D{name}%20-c%20lock_timeout%3D8s"
    dsn = settings.DATABASE_URL + f"{sep}options={opts}"
    from src.backend.core.run_registry import RunRegistry
    r = RunRegistry(dsn=dsn)
    try:
        yield r, admin, dsn
    finally:
        r.close()
        admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _seed(admin):
    """One folder, one test filed in it, one result of that test also filed
    in it — the shape both routes and assign_tests all reach."""
    folder = str(uuid.uuid4())
    other = str(uuid.uuid4())
    test_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO run_groups (group_id, name, org_id, created_by)"
        " VALUES (%s, 'F', %s, %s), (%s, 'F2', %s, %s)",
        (folder, ORG, USER, other, ORG, USER))
    admin.execute(
        "INSERT INTO tests (test_id, org_id, key_n, user_id, user_query,"
        " group_id, current_version) VALUES (%s, %s, 1, %s, 'q', %s, 1)",
        (test_id, ORG, USER, folder))
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, status, org_id, test_id,"
        " group_id) VALUES (%s, %s, 'passed', %s, %s, %s)",
        (run_id, USER, ORG, test_id, folder))
    return folder, other, test_id, run_id


def _race(dsn, test_id, target, victim):
    """Hold assign_tests' `tests` write, run `victim` on a thread, then issue
    assign_tests' `test_runs` write. Returns (victim_error, counterparty_error).

    The counterparty is assign_tests' two statements verbatim rather than a
    call to assign_tests, because the deadlock only exists while something
    sits BETWEEN them and the public method offers no seam."""
    errors = {}
    tx2 = psycopg.connect(dsn)
    try:
        tx2.execute(
            "UPDATE tests SET group_id = %s, updated_at = now()"
            " WHERE test_id = %s", (target, test_id))

        def run_victim():
            try:
                errors["victim"] = None
                victim()
            except Exception as e:                      # noqa: BLE001
                errors["victim"] = e

        t = threading.Thread(target=run_victim)
        t.start()
        # The victim is now blocked wanting a row tx2 holds, or — once it
        # takes its `tests` lock first — blocked before it holds anything.
        t.join(timeout=_SETTLE)
        try:
            tx2.execute(
                "UPDATE test_runs r SET group_id = %s FROM tests te"
                " WHERE te.test_id = r.test_id AND te.test_id = %s",
                (target, test_id))
            tx2.commit()
            errors["counterparty"] = None
        except Exception as e:                          # noqa: BLE001
            tx2.rollback()
            errors["counterparty"] = e
        t.join(timeout=30)
        assert not t.is_alive(), "victim thread never finished"
    finally:
        tx2.close()
    return errors["victim"], errors["counterparty"]


def test_assign_runs_does_not_deadlock_with_assign_tests(reg):
    """Filing a run while a concurrent write files its test.

    assign_runs' first two statements write `test_runs` and its third writes
    `tests`, so without a leading lock it holds the result row and then waits
    for the test row that the counterparty already holds — while the
    counterparty waits for the result row. One of the two dies."""
    r, admin, dsn = reg
    folder, other, test_id, run_id = _seed(admin)
    out = {}

    def victim():
        out["ok"] = r.assign_runs(ORG, USER, False, [run_id], other)

    err_v, err_c = _race(dsn, test_id, folder, victim)
    assert err_v is None, f"assign_runs aborted: {type(err_v).__name__}"
    assert err_c is None, f"counterparty aborted: {type(err_c).__name__}"
    assert out["ok"] is True


def test_delete_group_does_not_deadlock_with_assign_tests(reg):
    """Deleting a folder while a concurrent write moves a test out of it.

    delete_group's single DELETE cascades to `test_runs` and to `tests`, and
    on a fresh database it reaches the result row first — so it holds that row
    and waits for the test row the counterparty holds, while the counterparty
    waits for the result row.

    The end state is asserted too: waiting is only the right answer if the
    folder still goes away afterwards."""
    r, admin, dsn = reg
    folder, other, test_id, run_id = _seed(admin)
    out = {}

    def victim():
        out["ok"] = r.delete_group(ORG, USER, True, folder)

    err_v, err_c = _race(dsn, test_id, other, victim)
    assert err_v is None, f"delete_group aborted: {type(err_v).__name__}"
    assert err_c is None, f"counterparty aborted: {type(err_c).__name__}"
    assert out["ok"] is True
    left = admin.execute(
        "SELECT count(*) FROM run_groups WHERE group_id = %s",
        (folder,)).fetchone()[0]
    assert left == 0
