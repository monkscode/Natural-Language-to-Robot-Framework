"""RunRegistry.count_tests_by_health -- the number on each Tests-page tab.

GET /api/tests returns `total` for the ACTIVE health filter only, and the
page's All / Failing / Passing tabs each carry a count (spec section 7.2).
These counts must describe exactly the rows each tab would list, for the same
caller: a tab reading "Failing 3" above a page of 2 is the defect a count
exists to prevent. So every number here is checked two ways -- against an
explicit expected value, and against the `total` list_tests itself reports
for that tab under the same caller and filters.

Per-viewer by construction, as the rows already are: the counts read the same
caller-scoped lateral list_tests reads, so a failure the viewer cannot open
does not make their test count as failing.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (count_tests_by_health,
list_tests, _caller_results_lateral).
"""
import uuid
from unittest.mock import patch

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

ORG_A = "org-a"
ORG_B = "org-b"


@pytest.fixture
def reg():
    name = f"tests_counts_{uuid.uuid4().hex[:8]}"
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


# Direct SQL for the reason every registry reader test gives: these shapes
# need chosen statuses, owners and folders on rows record_start cannot write
# in one call.

_key = iter(range(1, 10_000))


def _test(admin, *, statuses, user_id="alice", org_id=ORG_A, group_id=None,
          query="q"):
    """One test with one version and one result per status, oldest first.
    `statuses=[]` is a test that has never run."""
    test_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO tests (test_id, org_id, key_n, user_id, user_email,"
        " user_query, current_version, group_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, 1, %s)",
        (test_id, org_id, next(_key), user_id, f"{user_id}@x.com", query,
         group_id))
    version_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_versions (version_id, test_id, n, user_query,"
        " robot_code, created_by) VALUES (%s, %s, 1, %s, '*** Tasks ***', %s)",
        (version_id, test_id, query, user_id))
    for i, status in enumerate(statuses):
        admin.execute(
            "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
            " status, org_id, test_id, test_version_id, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), user_id, f"{user_id}@x.com", query, status,
             org_id, test_id, version_id, f"2026-01-01T{10 + i:02d}:00:00Z"))
    return test_id


def _folder(admin, org=ORG_A):
    group_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO run_groups (group_id, name, org_id, created_by)"
        " VALUES (%s, 'Regression', %s, 'alice')", (group_id, org))
    return group_id


def _five_of_alices(admin):
    """One test in every health bucket, including BOTH shapes of not_run."""
    _test(admin, statuses=["passed"], query="passes")
    _test(admin, statuses=["passed", "failed"], query="fails")
    _test(admin, statuses=["error"], query="errors")
    _test(admin, statuses=["running"], query="in flight")
    _test(admin, statuses=[], query="never run")


MEMBER = dict(user_id="alice", org_id=ORG_A, folder_org_id=ORG_A,
              include_unowned=False)


def _totals(r, scope, **filters):
    return {
        "all": r.list_tests(health=None, **scope, **filters)[1],
        "passing": r.list_tests(health="passing", **scope, **filters)[1],
        "failing": r.list_tests(health="failing", **scope, **filters)[1],
    }


def test_counts_every_health_bucket_over_the_callers_tests(reg):
    """'error' counts as failing, as health reads it; a test still running
    and a test never run are in All and in neither of the other two."""
    r, admin = reg
    _five_of_alices(admin)

    assert r.count_tests_by_health(**MEMBER) == {
        "all": 5, "passing": 1, "failing": 2}


def _mixed_population(admin):
    """Rows that each caller shape below sees a DIFFERENT subset of."""
    folder = _folder(admin)
    _five_of_alices(admin)
    _test(admin, statuses=["failed"], user_id="bob")                    # private peer
    _test(admin, statuses=["passed"], user_id="bob", group_id=folder)   # published peer
    _test(admin, statuses=["failed"], user_id="carol", org_id=ORG_B)    # other org
    _test(admin, statuses=["passed"], user_id=None)                     # unowned


CALLERS = {
    "member": MEMBER,
    "org_admin": dict(user_id=None, org_id=ORG_A, folder_org_id=ORG_A,
                      include_unowned=False),
    "platform_admin": dict(user_id=None, org_id=None, folder_org_id=ORG_A,
                           include_unowned=True),
    "token_less": dict(user_id=None, org_id=None, folder_org_id=None,
                       include_unowned=True),
    "org_less": dict(user_id="alice", org_id=None, folder_org_id=None,
                     include_unowned=False),
}


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_each_count_equals_the_total_that_tab_lists(reg, caller):
    r, admin = reg
    _mixed_population(admin)

    scope = CALLERS[caller]
    assert r.count_tests_by_health(**scope) == _totals(r, scope)


def test_the_caller_shapes_above_really_see_different_sets(reg):
    """Guards the parametrised test against passing vacuously: if every
    shape saw the same rows, agreement would prove nothing about scoping."""
    r, admin = reg
    _mixed_population(admin)

    alls = {name: r.count_tests_by_health(**scope)["all"]
            for name, scope in CALLERS.items()}
    # member: own 5 + the published peer. org_admin: every ATTRIBUTED test
    # in the org. The two no-filter shapes: all 9, the unowned one included.
    # org_less: own tests only -- no org, so no folder publishes to them.
    assert alls == {"member": 6, "org_admin": 7, "platform_admin": 9,
                    "token_less": 9, "org_less": 5}


def test_counts_follow_the_search_and_the_folder_filter(reg):
    """The tabs count what the page is showing: a search or a folder narrows
    every tab's number, never only the active tab's."""
    r, admin = reg
    folder = _folder(admin)
    _test(admin, statuses=["passed"], query="checkout passes", group_id=folder)
    _test(admin, statuses=["failed"], query="checkout fails")
    _test(admin, statuses=["failed"], query="login fails", group_id=folder)

    searched = r.count_tests_by_health(q="checkout", **MEMBER)
    assert searched == {"all": 2, "passing": 1, "failing": 1}
    assert searched == _totals(r, MEMBER, q="checkout")

    filed = r.count_tests_by_health(group=folder, **MEMBER)
    assert filed == {"all": 2, "passing": 1, "failing": 1}
    assert filed == _totals(r, MEMBER, group=folder)

    unfiled = r.count_tests_by_health(group="ungrouped", **MEMBER)
    assert unfiled == {"all": 1, "passing": 0, "failing": 1}
    assert unfiled == _totals(r, MEMBER, group="ungrouped")


def test_counts_are_per_viewer(reg):
    """A member's UNPUBLISHED test re-run by their org_admin: the admin's
    failing result is invisible to the member, so for the member the test is
    passing, while the org_admin -- who can open both -- sees it failing."""
    r, admin = reg
    test_id = _test(admin, statuses=["passed"])
    version_id = admin.execute(
        "SELECT version_id FROM test_versions WHERE test_id = %s",
        (test_id,)).fetchone()[0]
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, test_id, test_version_id, created_at)"
        " VALUES (%s, 'adm', 'adm@x.com', 'q', 'failed', %s, %s, %s,"
        " '2026-01-01T23:00:00Z')",
        (str(uuid.uuid4()), ORG_A, test_id, version_id))

    assert r.count_tests_by_health(**MEMBER) == {
        "all": 1, "passing": 1, "failing": 0}
    assert r.count_tests_by_health(**CALLERS["org_admin"]) == {
        "all": 1, "passing": 0, "failing": 1}


def test_a_storage_error_answers_zeros_rather_than_raising(reg):
    """A page read must not break like a mutation -- list_tests swallows and
    returns ([], 0) for the same reason, so the two agree on failure too."""
    r, _admin = reg
    with patch.object(r._pool, "connection",
                      side_effect=psycopg.OperationalError("down")):
        assert r.count_tests_by_health(**MEMBER) == {
            "all": 0, "passing": 0, "failing": 0}
