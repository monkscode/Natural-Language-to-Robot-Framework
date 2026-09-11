"""The Tests page's per-row numbers are scoped to the results the caller may open.

Every run-derived value on a row — result_count, pass_count, last_status,
last_run_at, last_run_id, health, running, spark — used to be computed over
EVERY result of the test, while the drawer one URL deeper lists only the
results this caller may open (get_test_detail's run predicate, shipped
2026-09-10). The row and the drawer therefore described different sets, and
the row was the one that over-reported: it could show a failure the viewer can
open nowhere and hand out a last_run_id the server answers 404 for.

The shape is reachable in ordinary signed-in use, not only through the
token-less dev caller: a member's UNPUBLISHED test, re-run by their org_admin.
_VISIBLE_RUN_SQL admits the admin's run to the admin and refuses it to the
member, while _VISIBLE_TEST_SQL still shows the member their own test.

Two consequences are deliberate and are pinned here rather than left to be
rediscovered as bugs: the page's ORDER BY reads a narrowed last_run_at, so the
order is per-viewer and a test whose only results are invisible sorts last;
and the Failing/Passing tab counts become per-viewer too, as History's already
are.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (list_tests, get_test_detail,
_VISIBLE_RUN_SQL, _VISIBLE_TEST_SQL, _group_join_for_test_results).
"""
import uuid

import psycopg
import pytest

from src.backend.core.config import settings

pytestmark = pytest.mark.integration

ORG_A = "org-a"
MEMBER = "bob"
ADMIN = "adm"


@pytest.fixture
def reg():
    name = f"tests_scope_{uuid.uuid4().hex[:8]}"
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


# Seeding is direct SQL for the reason every other registry reader test gives:
# these shapes need a second user's result under someone else's test at a
# chosen timestamp, which one record_start call cannot produce.

def _test_row(admin, *, key_n, user_id=MEMBER, org_id=ORG_A, group_id=None,
              current_version=1):
    test_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO tests (test_id, org_id, key_n, user_id, user_email,"
        " user_query, current_version, group_id)"
        " VALUES (%s, %s, %s, %s, %s, 'q', %s, %s)",
        (test_id, org_id, key_n, user_id, f"{user_id}@x.com",
         current_version, group_id))
    return test_id


def _version_row(admin, test_id, n=1, robot_code="*** Tasks ***"):
    version_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_versions (version_id, test_id, n, user_query,"
        " robot_code, created_by) VALUES (%s, %s, %s, 'q', %s, %s)",
        (version_id, test_id, n, robot_code, MEMBER))
    return version_id


def _run_row(admin, test_id, version_id, status, created_at,
             user_id=MEMBER, org_id=ORG_A):
    run_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, test_id, test_version_id, created_at)"
        " VALUES (%s, %s, %s, 'q', %s, %s, %s, %s, %s)",
        (run_id, user_id, f"{user_id}@x.com", status, org_id, test_id,
         version_id, created_at))
    return run_id


def _folder(admin, org=ORG_A):
    group_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO run_groups (group_id, name, org_id, created_by)"
        " VALUES (%s, 'Regression', %s, %s)", (group_id, org, ADMIN))
    return group_id


def _member_rows(r, **kw):
    return r.list_tests(user_id=MEMBER, org_id=ORG_A, folder_org_id=ORG_A,
                        **kw)


def _reviewed_by_admin(admin):
    """A member's UNPUBLISHED test: their own passing result, then their
    org_admin's failing re-run of it. The admin's result is invisible to the
    member — the test is in no folder, so _VISIBLE_RUN_SQL leaves them the
    bare owner check."""
    test_id = _test_row(admin, key_n=1)
    version_id = _version_row(admin, test_id)
    mine = _run_row(admin, test_id, version_id, "passed",
                    "2026-01-01T10:00:00Z")
    theirs = _run_row(admin, test_id, version_id, "failed",
                      "2026-01-01T11:00:00Z", user_id=ADMIN)
    return test_id, version_id, mine, theirs


def test_row_numbers_count_only_results_the_caller_may_open(reg):
    """The whole worked example, on one row.

    Un-narrowed this row read result_count=2, last_status='failed',
    last_run_id=<the admin's run>, health='failing', spark=['pass','fail'] —
    every one of them describing a result the member's own drawer does not
    list and whose report the server refuses."""
    r, admin = reg
    test_id, _v, mine, theirs = _reviewed_by_admin(admin)

    rows, total = _member_rows(r)
    assert total == 1
    row = rows[0]
    assert row["test_id"] == test_id
    assert row["result_count"] == 1
    assert row["pass_count"] == 1
    assert row["last_status"] == "passed"
    assert row["last_run_id"] == mine
    assert row["last_run_id"] != theirs
    assert row["health"] == "passing"
    assert row["spark"] == ["pass"]
    assert row["running"] is False


def test_row_and_drawer_report_the_same_health(reg):
    """The drawer's own health dot, off the same definition.

    get_test_detail narrows its RESULTS list already; its health came off an
    un-narrowed lateral, so the drawer could list one passing result under a
    'failing' dot."""
    r, admin = reg
    test_id, _v, _mine, _theirs = _reviewed_by_admin(admin)

    rows, _ = _member_rows(r)
    detail = r.get_test_detail(test_id, user_id=MEMBER, org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail is not None
    assert detail["results_total"] == 1
    assert len(detail["results"]) == 1
    assert detail["test"]["health"] == "passing"
    assert detail["test"]["health"] == rows[0]["health"]


def test_health_filter_counts_are_per_viewer(reg):
    """The Failing tab, which reads the same narrowed health.

    The member's test is passing FOR THEM, so it appears under Passing and not
    under Failing — and the total has to agree with the page, or the tab shows
    a count it cannot fill."""
    r, admin = reg
    _reviewed_by_admin(admin)

    failing, failing_total = _member_rows(r, health="failing")
    passing, passing_total = _member_rows(r, health="passing")
    assert (failing, failing_total) == ([], 0)
    assert passing_total == 1
    assert len(passing) == 1


def test_running_is_scoped_to_results_the_caller_may_open(reg):
    """A peer's in-flight result must not spin the member's row."""
    r, admin = reg
    test_id = _test_row(admin, key_n=1)
    version_id = _version_row(admin, test_id)
    _run_row(admin, test_id, version_id, "passed", "2026-01-01T10:00:00Z")
    _run_row(admin, test_id, version_id, "running", "2026-01-01T11:00:00Z",
             user_id=ADMIN)

    rows, _ = _member_rows(r)
    assert rows[0]["running"] is False
    assert rows[0]["health"] == "passing"


def test_a_test_whose_only_results_are_invisible_reads_as_never_run(reg):
    """No result this caller may open is the same answer as no result.

    version_count and can_run are NOT run-derived and must not move with it:
    the version row and its code are the test's, not any result's."""
    r, admin = reg
    test_id = _test_row(admin, key_n=1)
    version_id = _version_row(admin, test_id)
    _run_row(admin, test_id, version_id, "failed", "2026-01-01T10:00:00Z",
             user_id=ADMIN)

    rows, total = _member_rows(r)
    assert total == 1
    row = rows[0]
    assert row["result_count"] == 0
    assert row["pass_count"] == 0
    assert row["last_status"] is None
    assert row["last_run_at"] is None
    assert row["last_run_id"] is None
    assert row["health"] == "not_run"
    assert row["spark"] == []
    assert row["version_count"] == 1
    assert row["can_run"] is True


def test_order_reads_the_narrowed_last_run_at(reg):
    """Case 27's order, over the narrowed value — so the page is ordered by
    dates the viewer can actually see.

    T1's newest result is the admin's and invisible; the member's own newest
    is older than T2's, so for the member T2 sorts first. An admin looking at
    the same two tests still sees T1 first, which is the point: the order is
    per-viewer now, and that is a visible change."""
    r, admin = reg
    t1 = _test_row(admin, key_n=1)
    v1 = _version_row(admin, t1)
    _run_row(admin, t1, v1, "passed", "2026-01-01T10:00:00Z")
    _run_row(admin, t1, v1, "failed", "2026-01-01T13:00:00Z", user_id=ADMIN)
    t2 = _test_row(admin, key_n=2)
    v2 = _version_row(admin, t2)
    _run_row(admin, t2, v2, "passed", "2026-01-01T11:00:00Z")

    rows, _ = _member_rows(r)
    assert [x["test_id"] for x in rows] == [t2, t1]


def test_a_test_with_no_visible_results_sorts_last(reg):
    """NULLS LAST stops doing nothing.

    Before the narrowing last_run_at was never NULL in practice — every test
    is minted with a run. Narrowed, a test whose only results belong to
    someone else genuinely has none for this caller, and it belongs at the
    bottom rather than sorted by a date they cannot see."""
    r, admin = reg
    hidden = _test_row(admin, key_n=1)
    vh = _version_row(admin, hidden)
    _run_row(admin, hidden, vh, "passed", "2026-01-01T23:00:00Z",
             user_id=ADMIN)
    mine = _test_row(admin, key_n=2)
    vm = _version_row(admin, mine)
    _run_row(admin, mine, vm, "passed", "2026-01-01T09:00:00Z")

    rows, _ = _member_rows(r)
    assert [x["test_id"] for x in rows] == [mine, hidden]


def test_a_folder_in_another_org_publishes_nothing_to_this_caller(reg):
    """The lateral's folder join is scoped to the caller's org, and that is
    load-bearing rather than defensive.

    "Published" means in a folder THIS CALLER can see, so a folder belonging
    to another org must resolve to NULL and publish nothing — the property
    _VISIBLE_RUN_SQL's own docstring says the join, not the predicate, is
    responsible for.

    The shape is reachable and documented: delete_group's docstring traces
    how record_start's rerun path writes a run whose own group_id names a
    folder while its org_id names a different org, and _group_join's COALESCE
    falls back to that run column when the test itself is unfiled. Here the
    peer's result carries an ORG_B folder on a test in ORG_A; without the org
    term on the join it would count in the member's row, publish a failure
    they cannot open, and do it through another tenant's folder."""
    r, admin = reg
    foreign = _folder(admin, org="org-b")
    test_id = _test_row(admin, key_n=1)
    version_id = _version_row(admin, test_id)
    _run_row(admin, test_id, version_id, "passed", "2026-01-01T10:00:00Z")
    peer = _run_row(admin, test_id, version_id, "failed",
                    "2026-01-01T11:00:00Z", user_id=ADMIN)
    admin.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                  (foreign, peer))

    rows, _ = _member_rows(r)
    row = rows[0]
    assert row["result_count"] == 1
    assert row["last_status"] == "passed"
    assert row["health"] == "passing"


def test_a_published_test_still_counts_every_result(reg):
    """The narrowing must not shrink what the org genuinely shares.

    Filing the test into a folder of the member's own org publishes it, and
    _VISIBLE_RUN_SQL then admits every attributed result of it — so the row
    keeps counting the whole test, including the peer's."""
    r, admin = reg
    folder = _folder(admin)
    test_id = _test_row(admin, key_n=1, user_id=ADMIN, group_id=folder)
    version_id = _version_row(admin, test_id)
    _run_row(admin, test_id, version_id, "passed", "2026-01-01T10:00:00Z",
             user_id=ADMIN)
    newest = _run_row(admin, test_id, version_id, "failed",
                      "2026-01-01T12:00:00Z", user_id=MEMBER)

    rows, total = _member_rows(r)
    assert total == 1
    row = rows[0]
    assert row["result_count"] == 2
    assert row["pass_count"] == 1
    assert row["last_run_id"] == newest
    assert row["health"] == "failing"
    assert row["spark"] == ["pass", "fail"]


def test_spark_still_stops_at_ten(reg):
    """The bound survives the rewrite.

    V0 took it with an inner LIMIT 10; the narrowed lateral aggregates the
    version's completed results and keeps the last ten, so the cap has to be
    asserted rather than assumed."""
    r, admin = reg
    test_id = _test_row(admin, key_n=1)
    version_id = _version_row(admin, test_id)
    for i in range(13):
        _run_row(admin, test_id, version_id,
                 "passed" if i else "failed",
                 f"2026-01-01T{10 + i:02d}:00:00Z")

    rows, _ = _member_rows(r)
    spark = rows[0]["spark"]
    assert len(spark) == 10
    # oldest first, and the dropped three are the OLDEST — the one 'failed'
    # was the first result written, so it must not survive the trim.
    assert spark == ["pass"] * 10
    assert rows[0]["result_count"] == 13
