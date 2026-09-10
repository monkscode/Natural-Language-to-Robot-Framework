"""RunRegistry.get_test_detail — one test, its versions, and a page of results.

The drawer's data source (spec section 6.2 / 7.3). Three things this file
exists to pin, because each is a place the shape can go quietly wrong:

- visibility is the SAME predicate the list uses (_VISIBLE_TEST_SQL), so a
  test the Tests page will not list must not be readable one URL deeper;
- every result names ITS OWN version (section 10 case 15), including the
  code-less migrated row whose test_version_id is NULL, which must render as
  no version rather than as the test's current one;
- versions and results are two independent one-to-many children of `tests`,
  and reading them in one join would multiply their counts together.

Referenced by: none (registry unit tests only).
Depends on: src/backend/core/run_registry.py (get_test_detail,
_VISIBLE_TEST_SQL, _group_join_for_tests).
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
    name = f"tests_detail_{uuid.uuid4().hex[:8]}"
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
# Seeding helpers -- direct SQL for the same reason
# test_run_registry_list_tests.py uses it: get_test_detail is a READER, and
# these shapes (several versions of one test, a result pointing at a
# superseded version, a result pointing at no version at all) are ones
# record_start cannot produce in a single call.
# ---------------------------------------------------------------------------

def _test_row(admin, test_id, *, key_n=1, user_id="alice", org_id=ORG_A,
              user_email="a@x.com", user_query="q", current_version=1,
              group_id=None, name=None):
    admin.execute(
        "INSERT INTO tests (test_id, org_id, key_n, user_id, user_email,"
        " user_query, current_version, group_id, name)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (test_id, org_id, key_n, user_id, user_email, user_query,
         current_version, group_id, name))


def _version_row(admin, test_id, n, robot_code="*** Tasks ***",
                 created_by="alice", user_query="q", reason=None,
                 created_at="2026-01-01T09:00:00Z"):
    version_id = str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_versions (version_id, test_id, n, user_query,"
        " robot_code, created_by, reason, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (version_id, test_id, n, user_query, robot_code, created_by, reason,
         created_at))
    return version_id


def _run_row(admin, test_id, version_id, status, created_at,
             run_id=None, user_id="alice", org_id=ORG_A,
             user_email="a@x.com"):
    run_id = run_id or str(uuid.uuid4())
    admin.execute(
        "INSERT INTO test_runs (run_id, user_id, user_email, user_query,"
        " status, org_id, test_id, test_version_id, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, user_id, user_email, "q", status, org_id, test_id,
         version_id, created_at))
    return run_id


def _folder(admin, folder_id, org=ORG_A, name="Regression"):
    admin.execute(
        "INSERT INTO run_groups (group_id, name, org_id, created_by)"
        " VALUES (%s, %s, %s, 'alice') ON CONFLICT DO NOTHING",
        (folder_id, name, org))


def _one_test_one_run(admin, *, status="passed", user_id="alice",
                      org_id=ORG_A, group_id=None, key_n=1):
    """The minimal shape: one test, one version, one result."""
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, key_n=key_n, user_id=user_id, org_id=org_id,
              group_id=group_id)
    version_id = _version_row(admin, test_id, 1)
    run_id = _run_row(admin, test_id, version_id, status,
                      "2026-01-01T10:00:00Z", user_id=user_id, org_id=org_id)
    return test_id, run_id


# ---------------------------------------------------------------------------
# Visibility -- the same predicate as the list, so the drawer can never open
# a test the Tests page would not have shown.
# ---------------------------------------------------------------------------

def test_the_owner_reads_their_own_unfiled_test(reg):
    r, admin = reg
    test_id, run_id = _one_test_one_run(admin)

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail is not None
    assert detail["test"]["test_id"] == test_id
    assert detail["test"]["user_query"] == "q"
    assert detail["test"]["user_id"] == "alice"
    assert detail["test"]["user_email"] == "a@x.com"
    assert detail["test"]["org_id"] == ORG_A
    assert detail["test"]["name"] is None
    assert detail["test"]["group_id"] is None
    assert detail["test"]["group_name"] is None
    assert detail["test"]["current_version"] == 1
    assert detail["test"]["created_at"]
    assert detail["test"]["updated_at"]
    assert detail["results_total"] == 1
    assert [x["run_id"] for x in detail["results"]] == [run_id]


def test_a_peer_reads_a_test_their_org_published_into_a_folder(reg):
    """_VISIBLE_TEST_SQL's second term: not the caller's test, but filed in a
    folder belonging to the caller's own org."""
    r, admin = reg
    folder_id = str(uuid.uuid4())
    _folder(admin, folder_id, org=ORG_A)
    test_id, _ = _one_test_one_run(admin, user_id="alice", group_id=folder_id)

    detail = r.get_test_detail(test_id, user_id="bob", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail is not None
    assert detail["test"]["group_id"] == folder_id
    assert detail["test"]["group_name"] == "Regression"


def test_a_foreign_orgs_test_is_not_readable(reg):
    """Someone else's test in another org. Note which term refuses it:
    _VISIBLE_TEST_SQL already does, because it is neither alice's nor filed
    in a folder her org owns. The te.org_id term is redundant HERE -- the
    test below is the one that isolates it."""
    r, admin = reg
    test_id, _ = _one_test_one_run(admin, user_id="carol", org_id=ORG_B)

    assert r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                             folder_org_id=ORG_A) is None


def test_the_callers_own_test_in_another_org_is_not_readable(reg):
    """The shape only `te.org_id = %s` refuses: te.user_id IS the caller, so
    _VISIBLE_TEST_SQL's owner half admits it and the org term is the sole
    refuser.

    Reachable, not hypothetical: _collapse_to_single moves a user into a
    team org and nothing rewrites tests.user_id, so a member's tests from
    their old personal org keep that org while they carry the new one."""
    r, admin = reg
    test_id, _ = _one_test_one_run(admin, user_id="alice", org_id=ORG_B)

    assert r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                             folder_org_id=ORG_A) is None
    # ...and it is still theirs when they are scoped to that org.
    assert r.get_test_detail(test_id, user_id="alice", org_id=ORG_B,
                             folder_org_id=ORG_B) is not None


def test_a_peers_unfiled_test_is_not_readable(reg):
    """Same org, but nobody published it -- the folder half of
    _VISIBLE_TEST_SQL cannot admit it and the owner half does not match."""
    r, admin = reg
    test_id, _ = _one_test_one_run(admin, user_id="alice")

    assert r.get_test_detail(test_id, user_id="bob", org_id=ORG_A,
                             folder_org_id=ORG_A) is None


def test_an_unowned_test_is_hidden_from_an_org_admin_and_shown_to_an_admin(reg):
    """include_unowned is the org_admin/platform-admin split list_tests makes:
    both have user_id None, and only the platform admin may read a row nobody
    owns."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, user_id=None, org_id=ORG_A)
    _version_row(admin, test_id, 1)

    # org_admin of org-a: no per-user narrowing, but unowned rows fail closed.
    assert r.get_test_detail(test_id, user_id=None, org_id=ORG_A,
                             folder_org_id=ORG_A,
                             include_unowned=False) is None
    # Validated platform admin: history_scope gives org_id None too.
    assert r.get_test_detail(test_id, user_id=None, org_id=None,
                             folder_org_id=None,
                             include_unowned=True) is not None


def test_an_unknown_test_id_reads_as_nothing(reg):
    r, admin = reg
    _one_test_one_run(admin)

    assert r.get_test_detail(str(uuid.uuid4()), user_id="alice",
                             org_id=ORG_A, folder_org_id=ORG_A) is None


# ---------------------------------------------------------------------------
# Versions -- whole, newest first, every field section 6.2 names.
# ---------------------------------------------------------------------------

def test_versions_come_whole_newest_first_with_every_field(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, current_version=3)
    _version_row(admin, test_id, 1, robot_code="v1 code", created_by="alice",
                 user_query="first", reason="imported",
                 created_at="2026-01-01T09:00:00Z")
    _version_row(admin, test_id, 2, robot_code="v2 code", created_by="bob",
                 user_query="second", reason="update",
                 created_at="2026-01-02T09:00:00Z")
    _version_row(admin, test_id, 3, robot_code="v3 code", created_by="carol",
                 user_query="third", reason=None,
                 created_at="2026-01-03T09:00:00Z")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert [v["n"] for v in detail["versions"]] == [3, 2, 1]
    newest = detail["versions"][0]
    assert newest["user_query"] == "third"
    assert newest["robot_code"] == "v3 code"
    assert newest["created_by"] == "carol"
    assert newest["reason"] is None
    assert newest["created_at"].startswith("2026-01-03")
    assert detail["versions"][1]["reason"] == "update"


def test_a_version_with_no_code_is_still_listed(reg):
    """can_run's falsy-code case (section 10 case 31) is a Tests-page concern;
    the drawer must still show the version rather than hide it."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id)
    _version_row(admin, test_id, 1, robot_code=None)

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert [v["n"] for v in detail["versions"]] == [1]
    assert detail["versions"][0]["robot_code"] is None


# ---------------------------------------------------------------------------
# Results -- paged, newest first, each naming its own version.
# ---------------------------------------------------------------------------

def test_results_page_newest_first_over_a_whole_test_total(reg):
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id)
    version_id = _version_row(admin, test_id, 1)
    ids = [
        _run_row(admin, test_id, version_id, "passed",
                 f"2026-01-0{d}T10:00:00Z")
        for d in range(1, 6)
    ]

    first = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                              folder_org_id=ORG_A, limit=2, offset=0)
    assert first["results_total"] == 5
    assert [x["run_id"] for x in first["results"]] == [ids[4], ids[3]]

    second = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A, limit=2, offset=2)
    # The total is the WHOLE test's result count, not the page's length.
    assert second["results_total"] == 5
    assert [x["run_id"] for x in second["results"]] == [ids[2], ids[1]]

    past_the_end = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                                     folder_org_id=ORG_A, limit=2, offset=99)
    assert past_the_end["results"] == []
    assert past_the_end["results_total"] == 5


def test_a_result_of_a_superseded_version_names_its_own_version(reg):
    """Section 10 case 15. The newest version is 2; a result of version 1 must
    still read as 1."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, current_version=2)
    v1 = _version_row(admin, test_id, 1)
    v2 = _version_row(admin, test_id, 2)
    old = _run_row(admin, test_id, v1, "failed", "2026-01-01T10:00:00Z")
    new = _run_row(admin, test_id, v2, "passed", "2026-01-02T10:00:00Z")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    by_run = {x["run_id"]: x for x in detail["results"]}
    assert by_run[old]["n"] == 1
    assert by_run[old]["status"] == "failed"
    assert by_run[new]["n"] == 2


def test_a_result_with_no_version_names_no_version(reg):
    """The code-less migrated row (D8): test_id set, test_version_id NULL. It
    must read as null, NOT as the test's current version -- rendering it as
    current would attribute code it never ran."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, current_version=2)
    _version_row(admin, test_id, 1)
    v2 = _version_row(admin, test_id, 2)
    codeless = _run_row(admin, test_id, None, "error",
                        "2026-01-01T10:00:00Z")
    versioned = _run_row(admin, test_id, v2, "passed",
                         "2026-01-02T10:00:00Z")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    by_run = {x["run_id"]: x for x in detail["results"]}
    assert by_run[codeless]["n"] is None
    assert by_run[versioned]["n"] == 2
    assert detail["results_total"] == 2


def test_failure_class_and_locator_are_null_placeholders(reg):
    """They are P3 columns (section 8) and do not exist on test_runs yet. The
    field is on the wire so the drawer can build its seam; the VALUE is null
    for every result until P3 writes one."""
    r, admin = reg
    test_id, _ = _one_test_one_run(admin, status="failed")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail["results"][0]["failure_class"] is None
    assert detail["results"][0]["failure_locator"] is None


def test_versions_and_results_do_not_multiply_each_other(reg):
    """Two independent one-to-many children of `tests`. Read through one join
    they fan out: 3 versions x 7 results reads as 21 of each."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, current_version=3)
    versions = [_version_row(admin, test_id, n) for n in (1, 2, 3)]
    for i in range(7):
        _run_row(admin, test_id, versions[i % 3], "passed",
                 f"2026-01-{i + 1:02d}T10:00:00Z")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A, limit=200)
    assert len(detail["versions"]) == 3
    assert detail["results_total"] == 7
    assert len(detail["results"]) == 7


def test_another_tests_versions_and_results_do_not_leak_into_this_one(reg):
    """Both child reads are scoped by test_id, and a sibling test in the same
    schema is what proves it -- with only one test present, dropping either
    WHERE clause gives the identical answer and pins nothing."""
    r, admin = reg
    mine, my_run = _one_test_one_run(admin, key_n=1)
    _one_test_one_run(admin, key_n=2)

    detail = r.get_test_detail(mine, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail["results_total"] == 1
    assert [x["run_id"] for x in detail["results"]] == [my_run]
    # The sibling has a version of its own, also numbered 1.
    assert len(detail["versions"]) == 1


# ---------------------------------------------------------------------------
# health -- the SAME rule list_tests applies, scoped to the current version.
# ---------------------------------------------------------------------------

def test_health_reads_the_current_versions_last_completed_result(reg):
    """Ruling R1 against ruling R2, in the one shape that tells them apart.

    Version 2 is current and passed. Version 1 was then RE-RUN afterwards
    and failed, so the test's most recent activity of any kind is a failure
    -- and health must still read "passing", because it is the CURRENT
    version's last completed result and nothing else. Ordering the failures
    LAST is what makes this test bite: with the newest result also being the
    current version's, dropping the version scoping entirely would give the
    same answer and the test would prove nothing."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, current_version=2)
    v1 = _version_row(admin, test_id, 1)
    v2 = _version_row(admin, test_id, 2)
    _run_row(admin, test_id, v2, "passed", "2026-01-01T10:00:00Z")
    for d in range(1, 10):
        _run_row(admin, test_id, v1, "failed", f"2026-02-{d:02d}T10:00:00Z")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail["test"]["health"] == "passing"
    # The timeline is whole-test and disagrees, which is R2 working as
    # specified rather than a contradiction: its newest row is a failure.
    assert detail["results"][0]["status"] == "failed"
    assert detail["results"][0]["n"] == 1


def test_health_folds_error_into_failing(reg):
    r, admin = reg
    test_id, _ = _one_test_one_run(admin, status="error")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail["test"]["health"] == "failing"


def test_health_is_not_run_while_the_only_result_is_still_running(reg):
    """Section 10 case 18: a stuck in-flight result cannot make a test read as
    failing, and 'generated' is not a completed result either."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id)
    version_id = _version_row(admin, test_id, 1)
    _run_row(admin, test_id, version_id, "running", "2026-01-01T10:00:00Z")
    _run_row(admin, test_id, version_id, "generated", "2026-01-02T10:00:00Z")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail["test"]["health"] == "not_run"
    # ...but both rows are still results of the test and appear in the
    # timeline. health and the timeline answer different questions.
    assert detail["results_total"] == 2


def test_health_ignores_results_of_other_versions(reg):
    """The counterpart to the R1 test above: the current version has no
    completed result of its own, so health is not_run even though an older
    version passed."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, current_version=2)
    v1 = _version_row(admin, test_id, 1)
    _version_row(admin, test_id, 2)
    _run_row(admin, test_id, v1, "passed", "2026-01-01T10:00:00Z")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail["test"]["health"] == "not_run"


def test_health_is_not_run_when_the_current_version_row_is_missing(reg):
    """current_version can point at a version row that does not exist (the
    same shape list_tests is tested against). The lookup is a LEFT JOIN, so
    the test still reads rather than vanishing."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, current_version=7)
    _version_row(admin, test_id, 1)

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A)
    assert detail is not None
    assert detail["test"]["health"] == "not_run"
    assert [v["n"] for v in detail["versions"]] == [1]


# ---------------------------------------------------------------------------
# The token-less dev caller (AUTH_ENFORCED off) -- section 10 case 19.
# ---------------------------------------------------------------------------

def test_the_token_less_caller_reads_any_test(reg):
    r, admin = reg
    test_id, _ = _one_test_one_run(admin, user_id="carol", org_id=ORG_B)

    detail = r.get_test_detail(test_id, user_id=None, org_id=None,
                               folder_org_id=None, include_unowned=True)
    assert detail is not None
    assert detail["test"]["test_id"] == test_id


# ---------------------------------------------------------------------------
# Result-level visibility -- the drawer may not offer a result that
# /api/history hides and the /reports gate then refuses.
#
# auth/ownership.py states the invariant these pin: "_VISIBLE_RUN_SQL and
# _OWNED_RUN_SQL carry the same `user_id IS NOT NULL` term, so no list can
# offer a row this refuses." The TEST gate above says nothing about the
# individual results underneath it, so the run predicate has to be applied
# here too -- against the same _group_join every other run-scoped read uses,
# so the two cannot drift into different answers.
#
# Reachable go-forward, not only from legacy rows: _attach_test's rerun_of
# branch returns the SOURCE's test_id whoever the caller is, and
# caller_can_access rule 1 admits the token-less AUTH_ENFORCED=off caller, so
# one "Run again" against an authored, foldered test lands an unattributed
# result underneath it.
# ---------------------------------------------------------------------------

def _authored_test_with_an_unattributed_result(admin, group_id=None):
    """An AUTHORED test (so it is visible) holding two results: one authored
    by its author, one with no user at all."""
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, user_id="alice", org_id=ORG_A,
              group_id=group_id)
    version_id = _version_row(admin, test_id, 1)
    authored = _run_row(admin, test_id, version_id, "passed",
                        "2026-01-01T10:00:00Z", user_id="alice")
    orphan = _run_row(admin, test_id, version_id, "passed",
                      "2026-01-01T11:00:00Z", user_id=None, user_email=None)
    return test_id, authored, orphan


def test_an_unattributed_result_is_hidden_from_a_peer_and_from_the_total(reg):
    """The measured gap. /api/history showed the peer 1 row; the drawer
    showed 2, counted 2, and set has_report on the one the /reports gate
    refuses."""
    r, admin = reg
    group_id = str(uuid.uuid4())
    _folder(admin, group_id)
    test_id, authored, orphan = _authored_test_with_an_unattributed_result(
        admin, group_id=group_id)

    detail = r.get_test_detail(test_id, user_id="bob", org_id=ORG_A,
                               folder_org_id=ORG_A, include_unowned=False)

    assert [x["run_id"] for x in detail["results"]] == [authored]
    # The count beside the page must apply the same predicate, or the drawer
    # advertises rows it will never hand over.
    assert detail["results_total"] == 1
    # Exactly what /api/history answers for the same caller.
    assert r.list_runs(user_id="bob", org_id=ORG_A,
                       folder_org_id=ORG_A)[1] == 1


def test_a_peers_authored_result_is_still_visible_through_the_folder(reg):
    """The other half: filing a test PUBLISHES its results, so a colleague's
    authored result must still reach the drawer. A fix that hid it would be
    an over-correction."""
    r, admin = reg
    group_id = str(uuid.uuid4())
    _folder(admin, group_id)
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, user_id="alice", org_id=ORG_A,
              group_id=group_id)
    version_id = _version_row(admin, test_id, 1)
    mine = _run_row(admin, test_id, version_id, "passed",
                    "2026-01-01T10:00:00Z", user_id="bob")
    peers = _run_row(admin, test_id, version_id, "failed",
                     "2026-01-01T11:00:00Z", user_id="alice")

    detail = r.get_test_detail(test_id, user_id="bob", org_id=ORG_A,
                               folder_org_id=ORG_A, include_unowned=False)

    assert set(x["run_id"] for x in detail["results"]) == {mine, peers}
    assert detail["results_total"] == 2


def test_an_unattributed_result_is_hidden_from_an_org_admin(reg):
    """An org_admin carries user_id None, so _VISIBLE_RUN_SQL never binds for
    them -- _OWNED_RUN_SQL is what fails them closed, exactly as it does in
    list_runs. caller_can_access refuses owner_id None to a same-org
    org_admin too (ownership.py rule 3 requires it non-NULL)."""
    r, admin = reg
    test_id, authored, orphan = _authored_test_with_an_unattributed_result(
        admin)

    detail = r.get_test_detail(test_id, user_id=None, org_id=ORG_A,
                               folder_org_id=ORG_A, include_unowned=False)

    assert [x["run_id"] for x in detail["results"]] == [authored]
    assert detail["results_total"] == 1


def test_an_unattributed_result_is_shown_to_a_platform_admin(reg):
    """Rule 2 allows it, so the list must offer it -- the invariant runs both
    ways."""
    r, admin = reg
    test_id, authored, orphan = _authored_test_with_an_unattributed_result(
        admin)

    detail = r.get_test_detail(test_id, user_id=None, org_id=None,
                               folder_org_id=ORG_A, include_unowned=True)

    assert set(x["run_id"] for x in detail["results"]) == {authored, orphan}
    assert detail["results_total"] == 2


def test_an_unattributed_result_is_hidden_from_the_tests_own_author(reg):
    """Owning the TEST is not authority over a result nobody owns. The author
    reading their own UNFILED test gets the same answer /api/history gives
    them, which is the whole point of reusing the run predicate."""
    r, admin = reg
    test_id, authored, orphan = _authored_test_with_an_unattributed_result(
        admin)

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A, include_unowned=False)

    assert [x["run_id"] for x in detail["results"]] == [authored]
    assert detail["results_total"] == 1


def test_a_foreign_org_result_is_hidden_even_from_its_own_author(reg):
    """The org term is load-bearing on its own. This result is authored by
    the caller, so _VISIBLE_RUN_SQL's first term admits it; only the separate
    t.org_id clause -- the one list_runs binds for the same caller -- keeps a
    row whose org and test disagree out of the drawer. caller_can_access
    rules 3 and 5 both refuse it, so offering it would be a list that lies.

    _attach_test's own docstring is the authority on how a test's runs come to
    span two concrete orgs; D6 aims at equality without making it an
    invariant."""
    r, admin = reg
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, user_id="alice", org_id=ORG_A)
    version_id = _version_row(admin, test_id, 1)
    home = _run_row(admin, test_id, version_id, "passed",
                    "2026-01-01T10:00:00Z", user_id="alice", org_id=ORG_A)
    foreign = _run_row(admin, test_id, version_id, "passed",
                       "2026-01-01T11:00:00Z", user_id="alice", org_id=ORG_B)

    detail = r.get_test_detail(test_id, user_id="alice", org_id=ORG_A,
                               folder_org_id=ORG_A, include_unowned=False)

    assert [x["run_id"] for x in detail["results"]] == [home]
    assert detail["results_total"] == 1


def test_the_token_less_caller_still_sees_every_result(reg):
    """Rule 1 exempts it from everything, and include_unowned=True is how
    that reaches this read."""
    r, admin = reg
    test_id, authored, orphan = _authored_test_with_an_unattributed_result(
        admin)

    detail = r.get_test_detail(test_id, user_id=None, org_id=None,
                               folder_org_id=None, include_unowned=True)

    assert set(x["run_id"] for x in detail["results"]) == {authored, orphan}
    assert detail["results_total"] == 2


def test_an_org_less_caller_sees_only_their_own_results_on_their_own_test(reg):
    """The `identified` half of _group_join, which only this caller shape
    reaches: a token carrying an identity but no org (history_scope gives it
    user_id set, org_id and folder_org_id None). ON FALSE resolves no folder,
    so nothing is published to them and _VISIBLE_RUN_SQL reduces to their own
    rows -- the answer list_runs gives the same caller, and the one
    caller_can_access gives too, since its no-org branch is owner-only. The
    unfiltered form would resolve the test's folder and hand them a
    colleague's result through it."""
    r, admin = reg
    group_id = str(uuid.uuid4())
    _folder(admin, group_id)
    test_id = str(uuid.uuid4())
    _test_row(admin, test_id, user_id="alice", org_id=ORG_A,
              group_id=group_id)
    version_id = _version_row(admin, test_id, 1)
    mine = _run_row(admin, test_id, version_id, "passed",
                    "2026-01-01T10:00:00Z", user_id="alice")
    _run_row(admin, test_id, version_id, "passed",
             "2026-01-01T11:00:00Z", user_id="bob")

    detail = r.get_test_detail(test_id, user_id="alice", org_id=None,
                               folder_org_id=None, include_unowned=False)

    assert [x["run_id"] for x in detail["results"]] == [mine]
    assert detail["results_total"] == 1
    assert r.list_runs(user_id="alice", org_id=None,
                       folder_org_id=None)[1] == 1
